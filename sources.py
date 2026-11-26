import asyncio
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import re
import signal
from tempfile import TemporaryDirectory
from urllib.parse import quote

from companies import Company, company, from_2gis, normalized, object_id, objects, text


BATCH_TIMEOUT = 110
ACCESS_MESSAGE = re.compile(r"captcha|капча|access denied|доступ запрещ[её]н|too many requests", re.I)
PARSER_ERROR = re.compile(r"(?m)^.*\bERROR\b|ошибка во время работы парсера|данные не получены", re.I)
NO_MATCHES = "точных совпадений нет"
SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
WORKER_ERRORS = re.compile(r"(?m)^(?:[\w]+\.)*([\w]*(?:Error|Exception))(?=:)|\b(net::ERR_[A-Z0-9_]+)\b")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class City:
    name: str
    code: str | None


@dataclass(frozen=True)
class Target:
    city: City
    query: str
    rubric_id: str | None = None


@dataclass(frozen=True)
class Issue:
    source: str
    code: str
    message: str

    def public(self) -> dict:
        return {"source": self.source, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class Batch:
    rows: list[Company]
    issue: Issue | None = None


class SourceError(RuntimeError):
    def __init__(self, issue: Issue) -> None:
        super().__init__(issue.message)
        self.issue = issue


@lru_cache(maxsize=4)
def russian_cities(path: Path) -> tuple[City, ...]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    cities = {
        normalized(entry["name"]): City(entry["name"], entry["code"])
        for entry in objects(entries)
        if entry.get("country_code") == "ru" and entry.get("domain") == "ru"
        and text(entry.get("name")) and SLUG.fullmatch(text(entry.get("code")))
    }
    if not cities:
        raise ValueError("В репозитории 2ГИС не найден каталог российских городов.")
    return tuple(sorted(cities.values(), key=lambda city: normalized(city.name)))


@lru_cache(maxsize=4)
def russian_rubrics(path: Path) -> tuple[tuple[str, str], ...]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    rubrics = [
        (entry["label"], entry["code"])
        for entry in entries.values()
        if entry.get("isRussian") and not entry.get("children")
        and object_id(entry.get("code")) and text(entry.get("label"))
        and normalized(entry["label"]) != "без рубрики"
    ]
    priority = {"кафе": 0, "аптеки": 1, "стоматологические центры": 2}
    if not rubrics:
        raise ValueError("В репозитории 2ГИС не найден каталог российских рубрик.")
    return tuple(sorted(rubrics, key=lambda item: (priority.get(normalized(item[0]), 3), normalized(item[0]))))


class MapSources:
    def __init__(self, root: Path, chrome: str, chrome_driver: str | None) -> None:
        self.root = root
        self.chrome = chrome
        self.chrome_driver = chrome_driver
        self.two_gis = root / "sources" / "parser-2gis"
        data = self.two_gis / "parser_2gis" / "data"
        self.cities = russian_cities(data / "cities.json")
        self.rubrics = russian_rubrics(data / "rubrics.json")

    def targets(self, source: str, city: str, query: str, offset: int):
        all_cities = normalized(city) == "все"
        if all_cities:
            start = offset % len(self.cities)
            cities = self.cities[start:] + self.cities[:start]
        else:
            match = next((entry for entry in self.cities if normalized(entry.name) == normalized(city)), None)
            if source == "2gis" and match is None:
                raise SourceError(Issue(source, "unknown_city", "Город отсутствует в каталоге репозитория 2ГИС."))
            cities = (match or City(city, None),)

        corrected_query = "Стоматология" if normalized(query) == "стамоталогия" else query
        rubrics = self.rubrics if normalized(query) == "все" else ((corrected_query, None),)
        # Visits every city for a rubric before the next rubric. Adjacent slots
        # use different rubrics when both input filters are set to "все".
        for round_no in range(len(rubrics)):
            for city_slot, location in enumerate(cities):
                term, rubric_id = rubrics[(round_no + city_slot) % len(rubrics)]
                yield Target(location, term, rubric_id)

    async def search(self, source: str, target: Target, count: int) -> Batch:
        with TemporaryDirectory(prefix="persermaps-") as folder:
            output = Path(folder) / "result.json"
            log = Path(folder) / "browser.log"
            argv = self._two_gis_argv(target, count, output) if source == "2gis" else self._yandex_argv(target, count, output)
            try:
                status = await run_browser(argv, log)
            except TimeoutError:
                return Batch([], Issue(source, "timeout", "Поиск на картах занял слишком много времени."))
            except OSError:
                return Batch([], Issue(source, "launch_error", "Не удалось запустить браузерный парсер."))

            if source == "2gis":
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 64 * 1024))
                    log_text = stream.read().decode(errors="replace")
                if ACCESS_MESSAGE.search(log_text):
                    return Batch([], Issue(source, "access_restricted", "2ГИС ограничил доступ с IP сервера."))
            load_error = None
            try:
                data = json.loads(output.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as error:
                data = None
                load_error = type(error).__name__

            if source == "2gis":
                rows = [entry for item in objects(data) if (entry := from_2gis(item, target.city.name))]
                if not status and not PARSER_ERROR.search(log_text) and isinstance(data, list):
                    if rows or NO_MATCHES in log_text.casefold():
                        return Batch(rows)
                console_errors = sorted({name or network_error for name, network_error in WORKER_ERRORS.findall(log_text)})
                logger.warning(
                    "2GIS worker: exit=%d output=%s cards=%d errors=%s",
                    status, load_error or type(data).__name__, len(rows), ",".join(console_errors) or "none",
                )
                return Batch(rows, Issue(source, "browser_result", f"2ГИС завершил запуск с кодом {status}. Подробности в журнале службы."))
            if source == "yandex" and not status and isinstance(data, dict):
                rows = []
                for record in objects(data.get("companies")):
                    identity, name = object_id(record.get("id")), text(record.get("name"))
                    if identity and name:
                        rows.append(company(f"yandex:org:{identity}", name, record["address"], record["phones"], record["socials"]))
                error = data.get("issue")
                return Batch(rows, Issue(source, error["code"], error["message"]) if error else None)
            return Batch([], Issue(source, "missing_output", "Парсер завершился без читаемого результата."))

    def _two_gis_argv(self, target: Target, count: int, output: Path) -> list[str]:
        url = f"https://2gis.ru/{target.city.code}/search/{quote(target.query, safe='')}"
        if target.rubric_id:
            url += f"/rubricId/{target.rubric_id}"
        return [
            str(self.root / "2gis-venv" / "bin" / "python"), str(self.two_gis / "parser-2gis.py"),
            "--url", url, "--output-path", str(output), "--format", "json",
            "--chrome.binary_path", self.chrome, "--chrome.headless", "yes",
            "--chrome.memory-limit", "512", "--parser.max-records", str(count),
            "--parser.skip-404-response", "yes",
        ]

    def _yandex_argv(self, target: Target, count: int, output: Path) -> list[str]:
        args = [
            str(self.root / "venv" / "bin" / "python"), str(self.root / "yandex_chrome.py"),
            "--browser", self.chrome, "--output", str(output),
            "--city", target.city.name, "--query", target.query, "--limit", str(count),
        ]
        if self.chrome_driver:
            args.extend(("--driver", self.chrome_driver))
        return args


async def run_browser(arguments: list[str], log: Path) -> int:
    with log.open("wb") as browser_log:
        process = await asyncio.create_subprocess_exec(
            *arguments, stdout=browser_log, stderr=browser_log, start_new_session=True,
        )
        try:
            return await asyncio.wait_for(process.wait(), timeout=BATCH_TIMEOUT)
        finally:
            # Chrome and its driver inherit this process group. Close orphaned
            # children even when the upstream CLI exits after an exception.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()

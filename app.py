import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from itertools import zip_longest
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Path as RoutePath, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from called import CalledCompanies
from companies import Company, merge_companies
from sources import Issue, MapSources, SourceError, Target


JOB_TIMEOUT = 270
JOB_HISTORY = 24
SEARCH_ROUNDS = 3
COMPANY_ID = r"^(?:2gis:(?:org|branch)|yandex:org):[0-9]{1,24}$"
SearchText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
JobStatus = Literal["running", "complete", "partial", "failed"]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: SearchText
    company: SearchText
    limit: int = Field(ge=1, le=100, strict=True)
    source: Literal["both", "2gis", "yandex"] = "both"


class SearchStarted(BaseModel):
    job_id: str
    status: Literal["running"] = "running"


class PublicCompany(BaseModel):
    id: str
    name: str
    address: str | None
    phones: list[str]
    socials: list[str]


class PublicIssue(BaseModel):
    source: str
    code: str
    message: str


class SearchResult(BaseModel):
    job_id: str
    status: JobStatus
    requested: int
    returned: int
    companies: list[PublicCompany]
    issues: list[PublicIssue]


class CalledResult(BaseModel):
    id: str
    called: Literal[True] = True
    called_at: str


@dataclass
class Job:
    id: str
    requested: int
    status: JobStatus = "running"
    companies: list[Company] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


async def collect(query: SearchRequest, sources: MapSources, store: CalledCompanies, job: Job) -> None:
    source_names = ("2gis", "yandex") if query.source == "both" else (query.source,)
    offset = secrets.randbelow(len(sources.cities))
    targets = {name: iter(sources.targets(name, query.city, query.company, offset)) for name in source_names}
    found: dict[str, list[Company]] = {name: [] for name in source_names}
    pending = set(source_names)
    previous: dict[str, tuple[Target, int, int]] = {}
    eligible: list[Company] = []

    try:
        async with asyncio.timeout(JOB_TIMEOUT):
            for round_no in range(SEARCH_ROUNDS):
                if not pending or (round_no > 0 and len(eligible) >= query.limit):
                    break
                for name in source_names:
                    if name not in pending:
                        continue
                    needed = max(1, query.limit - len(eligible))
                    if round_no == 0 and len(source_names) > 1 and name == source_names[0]:
                        needed = (needed + 1) // 2
                    count = min(180, needed + max(6, needed // 2))
                    try:
                        target = next(targets[name], None)
                    except SourceError as error:
                        job.issues.append(error.issue)
                        pending.remove(name)
                        continue
                    if target is None:
                        old_target, old_count, old_size = previous[name]
                        if old_size < old_count or old_count >= 180:
                            pending.remove(name)
                            continue
                        target, count = old_target, min(180, old_count * 2)

                    batch = await sources.search(name, target, count)
                    previous[name] = (target, count, len(batch.rows))
                    found[name].extend(batch.rows)
                    if batch.issue:
                        job.issues.append(batch.issue)
                        pending.remove(name)

                    alternating = [entry for pair in zip_longest(*found.values()) for entry in pair if entry]
                    eligible = await asyncio.to_thread(store.uncalled, merge_companies(alternating))
                    if round_no > 0 and len(eligible) >= query.limit:
                        break
    except TimeoutError:
        job.issues.append(Issue("service", "time_limit", "Истекло время поиска. Показаны собранные компании."))

    job.companies = await asyncio.to_thread(store.uncalled, eligible, query.limit)
    if len(job.companies) == query.limit and not job.issues:
        job.status = "complete"
    elif not job.companies and job.issues:
        job.status = "failed"
    else:
        job.status = "partial"


def create_app(source_override: MapSources | None = None) -> FastAPI:
    api_key = os.environ.get("PARSER_API_KEY", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", api_key):
        raise RuntimeError("Задайте PARSER_API_KEY длиной не менее 32 символов.")
    default_source = os.environ.get("PARSER_DEFAULT_SOURCE", "both")
    if default_source not in ("both", "2gis", "yandex"):
        raise RuntimeError("PARSER_DEFAULT_SOURCE должен быть both, 2gis или yandex.")
    web_password = os.environ.get("PARSER_WEB_PASSWORD", "")
    if web_password and not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", web_password):
        raise RuntimeError("Задайте PARSER_WEB_PASSWORD длиной не менее 20 символов.")
    home = Path(__file__).resolve().parent
    store = CalledCompanies(Path(os.environ.get("PARSER_DB_PATH", "data/called.sqlite3")))
    if source_override is None:
        chrome = os.environ.get("MAPS_CHROME_BINARY") or shutil.which("google-chrome-stable") or shutil.which("chromium")
        if not chrome:
            raise RuntimeError("Chrome не найден. Проверьте MAPS_CHROME_BINARY.")
        source_override = MapSources(home, chrome, os.environ.get("MAPS_CHROMEDRIVER") or None)
    sources = source_override
    jobs: dict[str, Job] = {}
    active: asyncio.Task | None = None
    bearer = HTTPBearer(auto_error=False)
    basic = HTTPBasic(auto_error=False)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await asyncio.to_thread(store.initialize)
        yield
        if active is not None and not active.done():
            active.cancel()
            with suppress(asyncio.CancelledError):
                await active

    app = FastAPI(title="Парсер компаний", version="1.0", lifespan=lifespan)

    def valid_web_login(credentials: HTTPBasicCredentials | None) -> bool:
        return bool(
            web_password and credentials
            and secrets.compare_digest(credentials.username.encode(), b"parser")
            and secrets.compare_digest(credentials.password.encode(), web_password.encode())
        )

    def require_key(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        web_login: Annotated[HTTPBasicCredentials | None, Depends(basic)],
    ) -> None:
        if credentials and secrets.compare_digest(credentials.credentials.encode(), api_key.encode()):
            return
        if valid_web_login(web_login):
            if request.method in ("POST", "PUT") and request.headers.get("sec-fetch-site") not in (None, "same-origin"):
                raise HTTPException(status_code=403, detail={"message": "Откройте страницу парсера и повторите действие."})
            return
        raise HTTPException(
            status_code=401, headers={"WWW-Authenticate": "Bearer"},
            detail={"code": "unauthorized", "message": "Требуется авторизация в парсере."},
        )

    app.mount("/assets", StaticFiles(directory=home / "web" / "assets", check_dir=False), name="assets")

    @app.get("/", include_in_schema=False)
    def web_page(credentials: Annotated[HTTPBasicCredentials | None, Depends(basic)]) -> FileResponse:
        if not valid_web_login(credentials):
            raise HTTPException(
                status_code=401, headers={"WWW-Authenticate": 'Basic realm="Company Parser"'},
                detail={"message": "Войдите с логином parser и паролем страницы."},
            )
        return FileResponse(home / "web" / "index.html", headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'",
        })

    @app.exception_handler(sqlite3.Error)
    async def storage_failure(_request, _error) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": {
            "code": "storage_unavailable", "message": "Хранилище отметок недоступно.",
        }})

    async def run(query: SearchRequest, job: Job) -> None:
        try:
            await collect(query, sources, store, job)
        except Exception:
            logging.exception("Search failed")
            job.issues.append(Issue("service", "search_error", "Не удалось завершить поиск. Проверьте журнал сервиса."))
            job.status = "failed"

    @app.get("/health")
    def health() -> dict:
        store.check()
        return {
            "status": "ok", "sources": ["2gis", "yandex"],
            "default_source": default_source, "catalog_cities": len(sources.cities),
        }

    @app.post("/v1/search", status_code=202, dependencies=[Depends(require_key)])
    async def start_search(query: SearchRequest) -> SearchStarted:
        nonlocal active
        if active is not None and not active.done():
            raise HTTPException(
                status_code=429, headers={"Retry-After": "5"},
                detail={"code": "search_busy", "message": "Парсер выполняет другой поиск. Повторите запрос позже."},
            )
        if len(jobs) == JOB_HISTORY:
            del jobs[next(iter(jobs))]
        if "source" not in query.model_fields_set:
            query = query.model_copy(update={"source": default_source})
        job = Job(uuid4().hex, query.limit)
        jobs[job.id] = job
        active = asyncio.create_task(run(query, job))
        return SearchStarted(job_id=job.id)

    @app.get("/v1/search/{job_id}", dependencies=[Depends(require_key)])
    async def search_result(job_id: Annotated[str, RoutePath(pattern=r"^[a-f0-9]{32}$")]) -> SearchResult:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "job_unavailable", "message": "Запустите поиск заново."})
        status = job.status
        companies = await asyncio.to_thread(store.uncalled, job.companies) if status != "running" else []
        if status == "complete" and len(companies) < job.requested:
            status = "partial"
        return SearchResult(
            job_id=job.id, status=status, requested=job.requested, returned=len(companies),
            companies=[PublicCompany(**company.public()) for company in companies],
            issues=[PublicIssue(**issue.public()) for issue in job.issues],
        )

    @app.put("/v1/companies/{company_id}/called", dependencies=[Depends(require_key)])
    def mark_called(company_id: Annotated[str, RoutePath(pattern=COMPANY_ID)]) -> CalledResult:
        return CalledResult(id=company_id, called_at=store.mark(company_id))

    return app

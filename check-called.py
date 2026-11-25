"""Exercise the installed API on loopback with a disposable SQLite database."""

import asyncio
from contextlib import asynccontextmanager, closing
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import secrets
import socket
from tempfile import TemporaryDirectory
from urllib.parse import quote

import uvicorn

from app import create_app
from companies import company
from sources import Batch, City, Target


class FixtureSources:
    cities = (City("Таганрог", "taganrog"),)

    def targets(self, _source, _city, query, _offset):
        yield Target(self.cities[0], query)

    async def search(self, source, _target, _count):
        prefix = "2gis:org:9" if source == "2gis" else "yandex:org:8"
        return Batch([
            company(prefix + "001", "Тестовая стоматология", "Таганрог, Тестовая улица, 1",
                    ["+70000000001"], []),
            company(prefix + "002", "Вторая стоматология", "Таганрог, Тестовая улица, 2",
                    ["+70000000002"], []),
        ])


@asynccontextmanager
async def running_api():
    server = uvicorn.Server(uvicorn.Config(create_app(FixtureSources()), log_level="warning", access_log=False))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if task.done():
                    task.result()
                    raise RuntimeError("Тестовый API завершился при запуске.")
                if server.started:
                    break
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Тестовый API не запустился.")
            yield port
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 6)


def request(port, key, method, path, expected, payload=None):
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as connection:
        connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        data = json.load(response)
        assert response.status == expected, (method, path, response.status, data)
        return data


async def search(port, key, source):
    started = await asyncio.to_thread(request, port, key, "POST", "/v1/search", 202, {
        "city": "Таганрог", "company": "Стоматология", "limit": 1, "source": source,
    })
    path = "/v1/search/" + started["job_id"]
    for _ in range(100):
        result = await asyncio.to_thread(request, port, key, "GET", path, 200)
        if result["status"] != "running":
            assert result["status"] == "complete" and result["returned"] == 1, result
            return path, result["companies"][0]["id"]
        await asyncio.sleep(0.05)
    raise RuntimeError("Тестовый поиск не завершился.")


async def main():
    with TemporaryDirectory(prefix="persermaps-called-check-") as directory:
        key = secrets.token_urlsafe(32)
        os.environ["PARSER_API_KEY"] = key
        os.environ["PARSER_DB_PATH"] = str(Path(directory) / "called.sqlite3")
        async with running_api() as port:
            result_path, identity = await search(port, key, "both")
            assert identity == "2gis:org:9001", identity
            mark_path = f"/v1/companies/{quote(identity, safe='')}/called"
            marked = await asyncio.to_thread(request, port, key, "PUT", mark_path, 200)
            assert marked["called"] is True
            repeated = await asyncio.to_thread(request, port, key, "PUT", mark_path, 200)
            assert marked == repeated
            updated = await asyncio.to_thread(request, port, key, "GET", result_path, 200)
            assert updated["returned"] == 0, updated
            print("OK: отметка скрыла карточку, повторная отправка сохранила отметку.")

        async with running_api() as port:
            _, identity = await search(port, key, "2gis")
            assert identity == "2gis:org:9002", identity
            _, identity = await search(port, key, "yandex")
            assert identity == "yandex:org:8002", identity
            print("OK: после перезапуска отметка исключила компанию из обоих источников на тестовых карточках.")


if __name__ == "__main__":
    asyncio.run(main())

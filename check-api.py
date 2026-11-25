import argparse
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def call(base: str, path: str, key: str, payload: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    data = json.dumps(payload).encode() if payload is not None else None
    with urlopen(Request(base + path, data=data, headers=headers), timeout=12) as response:
        return json.load(response)


def start() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=("default", "both", "2gis", "yandex"))
    parser.add_argument("city")
    parser.add_argument("company")
    parser.add_argument("limit", type=int)
    parser.add_argument("--env", type=Path, default=Path("/etc/persermaps-github.env"))
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    lines = args.env.read_text(encoding="utf-8").splitlines()
    key = next((line.partition("=")[2] for line in lines if line.startswith("PARSER_API_KEY=")), "")
    if not key:
        raise SystemExit("В файле конфигурации отсутствует ключ парсера.")
    payload = {"city": args.city, "company": args.company, "limit": args.limit}
    if args.source != "default":
        payload["source"] = args.source
    try:
        started = call(args.base.rstrip("/"), "/v1/search", key, payload)
        print(f"Поиск запущен: {started['job_id']}", flush=True)
        finish_by = time.monotonic() + 290
        while True:
            result = call(args.base.rstrip("/"), "/v1/search/" + started["job_id"], key)
            if result["status"] != "running":
                break
            if time.monotonic() >= finish_by:
                raise SystemExit("Поиск не завершился за 290 с.")
            time.sleep(2)
    except HTTPError as error:
        print(f"HTTP {error.code}")
        try:
            print(json.dumps(json.load(error).get("detail"), ensure_ascii=False))
        except (ValueError, AttributeError):
            pass
        return 1
    except URLError:
        print("API парсера недоступен по указанному адресу.")
        return 1

    companies = result["companies"]
    summary = {
        "status": result["status"], "source": args.source,
        "requested": result["requested"], "returned": result["returned"],
        "with_address": sum(bool(row["address"]) for row in companies),
        "with_phone": sum(bool(row["phones"]) for row in companies),
        "with_social": sum(bool(row["socials"]) for row in companies),
        "issues": result["issues"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.show:
        for row in companies:
            print(f"\nНазвание компании: {row['name']}")
            print(f"Точный адрес: {row['address'] or '—'}")
            print(f"Номера телефонов: {'; '.join(row['phones']) or '—'}")
            print(f"Соцсети: {'; '.join(row['socials']) or '—'}")
    return int(not companies or bool(summary["issues"]))


if __name__ == "__main__":
    raise SystemExit(start())

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from companies import Company


class CalledCompanies:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS called_companies ("
                "company_id TEXT PRIMARY KEY, called_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS company_aliases ("
                "company_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (company_id, alias))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS company_aliases_by_alias ON company_aliases(alias)")

    def mark(self, company_id: str) -> str:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO called_companies VALUES (?, ?) "
                "ON CONFLICT(company_id) DO NOTHING",
                (company_id, now),
            )
            (called_at,) = db.execute(
                "SELECT called_at FROM called_companies WHERE company_id = ?",
                (company_id,),
            ).fetchone()
        return called_at

    def check(self) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("SELECT 1 FROM called_companies LIMIT 1").fetchone()

    def uncalled(self, companies: list[Company], save_limit: int | None = None) -> list[Company]:
        aliases = sorted(set().union(*(company.aliases for company in companies)))
        if not aliases:
            return []
        with closing(sqlite3.connect(self.path)) as db, db:
            if save_limit is not None:
                db.execute("BEGIN IMMEDIATE")
            blocked: set[str] = set()
            for start in range(0, len(aliases), 400):
                block = aliases[start:start + 400]
                placeholders = ",".join("?" for _ in block)
                blocked.update(alias for (alias,) in db.execute(
                    f"SELECT company_id FROM called_companies WHERE company_id IN ({placeholders}) "
                    "UNION SELECT company_aliases.alias FROM company_aliases "
                    "JOIN called_companies USING (company_id) "
                    f"WHERE company_aliases.alias IN ({placeholders})", (*block, *block),
                ))
            eligible = [company for company in companies if company.aliases.isdisjoint(blocked)]
            if save_limit is not None:
                eligible = eligible[:save_limit]
                db.executemany(
                    "INSERT INTO company_aliases VALUES (?, ?) ON CONFLICT DO NOTHING",
                    [(company.id, alias) for company in eligible for alias in company.aliases],
                )
            return eligible

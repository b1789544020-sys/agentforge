#!/usr/bin/env python3
"""Create a consistent SQLite or PostgreSQL backup without shell injection."""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def database_url(explicit: str | None = None) -> str:
    url = explicit or os.environ.get("AGENTFORGE_SYNC_DATABASE_URL") or os.environ.get("AGENTFORGE_DATABASE_URL")
    if not url:
        raise ValueError("AGENTFORGE_DATABASE_URL is required")
    return url


def sqlite_path(url: str) -> Path:
    prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
    prefix = next((item for item in prefixes if url.startswith(item)), None)
    if prefix is None:
        raise ValueError("not a SQLite URL")
    raw = url[len(prefix):].split("?", 1)[0]
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        raise ValueError("in-memory/URI SQLite databases require an explicit file URL")
    path = Path(raw)
    if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
        path = Path(raw[1:])
    return path.expanduser().resolve()


def libpq_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def backup_sqlite(source: Path, output: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src:
        with sqlite3.connect(str(output)) as dst:
            src.backup(dst)


def backup_database(url: str, output: Path) -> None:
    if url.startswith("sqlite"):
        backup_sqlite(sqlite_path(url), output)
        return
    if url.startswith("postgresql"):
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(output), libpq_url(url)],
            check=True,
        )
        return
    raise ValueError("only SQLite and PostgreSQL URLs are supported")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    url = database_url(args.database_url)
    suffix = ".sqlite3" if url.startswith("sqlite") else ".dump"
    output = args.output or Path("backups") / (
        "agentforge-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + suffix
    )
    backup_database(url, output)
    print(output.resolve())


if __name__ == "__main__":
    main()

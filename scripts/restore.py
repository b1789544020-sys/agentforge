#!/usr/bin/env python3
"""Restore a backup after writers stop. Requires explicit --yes confirmation."""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
from pathlib import Path

from backup import backup_sqlite, database_url, libpq_url, sqlite_path


def restore_sqlite(backup: Path, destination: Path) -> None:
    if not backup.is_file():
        raise FileNotFoundError(f"backup not found: {backup}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(backup)) as src, sqlite3.connect(str(destination)) as dst:
        src.backup(dst)


def restore_database(url: str, backup: Path) -> None:
    if url.startswith("sqlite"):
        restore_sqlite(backup, sqlite_path(url))
        return
    if url.startswith("postgresql"):
        subprocess.run(
            ["pg_restore", "--clean", "--if-exists", "--exit-on-error", "--dbname", libpq_url(url), str(backup)],
            check=True,
        )
        return
    raise ValueError("only SQLite and PostgreSQL URLs are supported")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--database-url")
    parser.add_argument("--yes", action="store_true", help="confirm destructive restore")
    args = parser.parse_args()
    if not args.yes:
        parser.error("restore is destructive; stop writers and pass --yes after verification")
    restore_database(database_url(args.database_url), args.input)
    print("restore completed; run migration/version checks before starting writers")


if __name__ == "__main__":
    main()

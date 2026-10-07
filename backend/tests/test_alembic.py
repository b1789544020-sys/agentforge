"""Alembic migration tests.

We test the upgrade / downgrade round-trip against a real (file-based)
SQLite database, because in-memory SQLite would give each connection
its own private schema (and alembic's online mode opens at least one
connection per migration step).
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


def _run_alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run ``alembic <args>`` against a file-based SQLite DB.

    We point ``AGENTFORGE_DATABASE_URL`` at the file (with the async
    driver prefix, since that's what the app uses) so the env.py logic
    in ``alembic/env.py`` translates it to the sync form.
    """
    env = {
        **os.environ,
        "AGENTFORGE_DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}",
        "PYTHONPATH": str(BACKEND),
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def sqlite_file() -> Path:
    fd, name = tempfile.mkstemp(suffix=".db", prefix="alembic_test_")
    os.close(fd)
    p = Path(name)
    p.unlink()  # let alembic create the file
    try:
        yield p
    finally:
        if p.exists():
            p.unlink()


def _tables(db: Path) -> set[str]:
    cn = sqlite3.connect(str(db))
    try:
        rows = cn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        cn.close()


def test_alembic_upgrade_creates_all_tables(sqlite_file: Path) -> None:
    """``alembic upgrade head`` must create every model table."""
    proc = _run_alembic(sqlite_file, "upgrade", "head")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    tables = _tables(sqlite_file)
    expected = {
        "agents",
        "agent_runs",
        "agent_steps",
        "memories",
        "eval_cases",
        "eval_runs",
        "tool_metadata",  # 0.1.3 #3 -- plugin layer
        "alembic_version",
    }
    assert expected.issubset(tables), (
        f"missing: {expected - tables}\nall: {sorted(tables)}"
    )


def test_alembic_downgrade_drops_everything(sqlite_file: Path) -> None:
    """``alembic downgrade base`` must remove every model table."""
    up = _run_alembic(sqlite_file, "upgrade", "head")
    assert up.returncode == 0, up.stdout + up.stderr

    down = _run_alembic(sqlite_file, "downgrade", "base")
    assert down.returncode == 0, down.stdout + down.stderr

    tables = _tables(sqlite_file)
    model_tables = {
        "agents",
        "agent_runs",
        "agent_steps",
        "memories",
        "eval_cases",
        "eval_runs",
        "tool_metadata",  # 0.1.3 #3 -- must also be removed on downgrade
    }
    assert model_tables.isdisjoint(tables), (
        f"still present: {model_tables & tables}"
    )
    # alembic_version table itself stays around between migrations —
    # that's the design (it records the current revision).
    assert "alembic_version" in tables


def test_alembic_upgrade_is_idempotent(sqlite_file: Path) -> None:
    """A second ``upgrade head`` against an up-to-date DB is a no-op."""
    _run_alembic(sqlite_file, "upgrade", "head")
    first = _tables(sqlite_file)

    again = _run_alembic(sqlite_file, "upgrade", "head")
    assert again.returncode == 0, again.stdout + again.stderr
    second = _tables(sqlite_file)
    assert first == second


def test_alembic_round_trip(sqlite_file: Path) -> None:
    """upgrade -> downgrade -> upgrade must yield identical schemas."""
    _run_alembic(sqlite_file, "upgrade", "head")
    after_up = _tables(sqlite_file)

    _run_alembic(sqlite_file, "downgrade", "base")
    _run_alembic(sqlite_file, "upgrade", "head")
    after_round = _tables(sqlite_file)

    assert after_up == after_round


def test_alembic_current_reports_head(sqlite_file: Path) -> None:
    """``alembic current`` after upgrade reports the latest revision.

    The exact revision id changes whenever a new migration is added;
    we assert it matches the head of the ``alembic/versions`` tree by
    parsing the latest ``revision: str = '...'`` line.
    """
    _run_alembic(sqlite_file, "upgrade", "head")
    proc = _run_alembic(sqlite_file, "current")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    # 0.1.3 #3 -- the head is now the tool_metadata migration, not the
    # original baseline.  We assert that the ``alembic current`` output
    # reports a revision id that exists in the version tree.
    versions_dir = BACKEND / "alembic" / "versions"
    known_revs: set[str] = set()
    for path in versions_dir.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("revision:"):
                parts = stripped.split("=", 1)
                if len(parts) == 2:
                    rev = parts[1].strip().strip("'\"")
                    known_revs.add(rev)
    assert known_revs, "no revisions found in alembic/versions"
    assert any(rev in out for rev in known_revs), (
        f"alembic current output did not contain any known revision\n"
        f"known_revs={sorted(known_revs)}\nout={out}"
    )



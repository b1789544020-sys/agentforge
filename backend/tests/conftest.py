"""Shared pytest fixtures."""
from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Use an isolated file before any application import so config picks it up.
# An in-memory database uses StaticPool in app.database, which makes concurrent
# AsyncSessions share one SQLite connection and lets their transactions
# interfere.  A per-run file gives each session a real, independent connection.
_TEST_TEMP_DIR = tempfile.TemporaryDirectory(prefix="agentforge-pytest-", ignore_cleanup_errors=True)
_TEST_DB = Path(_TEST_TEMP_DIR.name) / "global.sqlite3"
os.environ.setdefault(
    "AGENTFORGE_DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DB.as_posix()}"
)
os.environ.setdefault("AGENTFORGE_LLM_PROVIDER", "mock")
# Most tests use Base.metadata.create_all through the fixture below.  Running
# Alembic again in every TestClient lifespan would replay the baseline over
# those tables.  Dedicated Alembic tests still run real migrations explicitly.
os.environ.setdefault("AGENTFORGE_RUN_MIGRATIONS", "false")

from sqlalchemy import event  # noqa: E402  (after env setup)

from app.database import Base, async_engine, get_session  # noqa: E402
from app.main import create_app  # noqa: E402


if async_engine.url.get_backend_name() == "sqlite":

    @event.listens_for(async_engine.sync_engine, "connect")
    def _set_global_sqlite_pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    # A per-test file gives concurrent AsyncSessions independent connections
    # without shared-cache lifetime coupling. In particular, closing the last
    # connection to a named in-memory database destroys its schema; the memory
    # cleanup test closes and reopens its session on every background cycle.
    # Install busy_timeout on every connection because PRAGMA values are local.
    from sqlalchemy import event

    db_path = Path(_TEST_TEMP_DIR.name) / f"db-session-{uuid.uuid4().hex}.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_busy_timeout(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def test_engine_factory():
    """Yield ``(engine, sessionmaker)`` against the shared-cache
    in-memory database so individual tests can drive the eval runner
    with their own sessionmaker (the runner defaults to
    ``AsyncSessionLocal`` which is bound to a *different* engine
    in tests).  Also monkey-patches ``app.database.AsyncSessionLocal``
    so the runner's default fallback shares data, *and* the
    ``async_session_factory`` alias that tests import directly.
    """
    import app.database as _db_mod

    from sqlalchemy import event

    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:agentforge_runner_{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_busy_timeout(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # Patch the import names that the eval runner and tests resolve
    # at call time.  ``app.database.AsyncSessionLocal`` is read by
    # the runner's lazy fallback; ``app.database.async_session_factory``
    # is what the test modules import directly (``from app.database
    # import async_session_factory``).
    original_db = _db_mod.AsyncSessionLocal
    original_factory = getattr(_db_mod, "async_session_factory", None)
    _db_mod.AsyncSessionLocal = Session  # type: ignore[attr-defined]
    _db_mod.async_session_factory = Session  # type: ignore[attr-defined]
    try:
        yield engine, Session
    finally:
        _db_mod.AsyncSessionLocal = original_db
        if original_factory is not None:
            _db_mod.async_session_factory = original_factory
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _ensure_schema():
    """Create the schema on the global async engine before every test.

    Some test modules (e.g. ``test_api.py``) build a FastAPI app inline via
    ``create_app()`` and do not request the ``app`` fixture, so we need an
    autouse hook to guarantee the tables exist regardless of how the app is
    obtained.
    """
    from app.database import Base, async_engine

    # Make sure models are registered.
    from app import models  # noqa: F401

    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    # The global engine is shared by the whole pytest process.  Recreate the
    # schema at the start of each test and drop it afterwards so users, agents
    # and other rows cannot leak into later tests on any database backend.
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def app():
    """Create a FastAPI app instance with DB tables created."""
    application = create_app()
    yield application

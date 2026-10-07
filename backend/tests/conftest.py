"""Shared pytest fixtures."""
from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Force an in-memory DB before any application import so config picks it up.
os.environ.setdefault("AGENTFORGE_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("AGENTFORGE_LLM_PROVIDER", "mock")

from app.database import Base, get_session  # noqa: E402  (after env setup)
from app.main import create_app  # noqa: E402


@pytest.fixture(scope="session")
def event_loop() -> AsyncIterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    # Concurrent sessions against a single in-memory SQLite is fragile
    # under aiosqlite (each fresh connection sees a private DB, and
    # StaticPool + WAL still races on writes).  The robust fix is to
    # use SQLite's shared-cache URI so *every* connection points at
    # the same in-memory database, plus a connect-listener that
    # installs ``busy_timeout`` on every new connection (PRAGMA
    # values are per-connection in SQLite).  This lets the eval
    # runner fan out cases onto fresh sessions without
    # "database is locked" errors and lets the memory cleanup
    # background task share the test's in-memory database.
    from sqlalchemy import event

    engine = create_async_engine(
        "sqlite+aiosqlite:///file:agentforge_conftest?mode=memory&cache=shared&uri=true",
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
        "sqlite+aiosqlite:///file:agentforge_runner?mode=memory&cache=shared&uri=true",
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
    # Drop tables after each test for isolation when using a file-backed DB.
    if not async_engine.url.database or async_engine.url.database == ":memory:":
        # In-memory DB: nothing to drop; the engine is shared across tests.
        return
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def app():
    """Create a FastAPI app instance with DB tables created."""
    application = create_app()
    yield application

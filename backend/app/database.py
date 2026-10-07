"""Database connection & session management.

We expose both an async engine (for the FastAPI request lifecycle) and a
synchronous engine (for tests, CLI tools, and migrations).
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    """Common SQLAlchemy declarative base for all ORM models."""


# ---- Async engine -------------------------------------------------------
# Detect SQLite so we can use the right pooling strategy.  aiosqlite
# always uses ``NullPool`` and rejects ``pool_size`` / ``max_overflow``,
# so those args must only be passed for non-SQLite backends.
_engine_kwargs: dict = {
    "echo": settings.db_echo,
    "future": True,
}
_is_sqlite = settings.database_url.startswith("sqlite")
if _is_sqlite and (
    settings.database_url.endswith(":memory:")
    or "mode=memory" in settings.database_url
):
    _engine_kwargs["poolclass"] = StaticPool
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
elif not _is_sqlite:
    _engine_kwargs["pool_size"] = settings.db_pool_size
    _engine_kwargs["max_overflow"] = settings.db_max_overflow

async_engine = create_async_engine(settings.database_url, **_engine_kwargs)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=async_engine,
    expire_on_commit=False,
    autoflush=False,
    class_=AsyncSession,
)

# Aliases preferred by some call sites (legacy / import naming variants).
async_session_factory = AsyncSessionLocal
async_session = AsyncSessionLocal


# ---- Sync engine (used by migrations & tests) ---------------------------
sync_engine = create_engine(
    settings.sync_database_url,
    echo=settings.db_echo,
    future=True,
    pool_pre_ping=True,
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


async def get_db() -> Generator[AsyncSession, None, None]:
    """FastAPI dependency that yields an :class:`AsyncSession`."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# Backwards-compatible alias (some tests/legacy code refers to ``get_session``).
get_session = get_db


@contextmanager
def get_sync_db() -> Generator[Session, None, None]:
    """Context manager that yields a synchronous :class:`Session`."""
    session = SyncSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def init_db() -> None:
    """Ensure the database schema is up to date.

    We delegate to Alembic so a single source of truth (the versioned
    migrations under ``backend/alembic/versions/``) owns the schema.
    For a brand-new database, this runs ``upgrade head`` to create
    every table; for an existing database, it is a no-op when the
    schema is already at head.

    The Alembic ``env.py`` reads ``AGENTFORGE_DATABASE_URL`` directly,
    so we invoke Alembic via the command-line entry point so the env
    var propagation / ``lru_cache`` ordering is identical to what a
    developer would experience running ``alembic upgrade head``.
    """
    import os
    import sys
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    backend_dir = Path(__file__).resolve().parent.parent
    ini_path = backend_dir / "alembic.ini"
    if not ini_path.exists():
        # No alembic configuration (e.g. someone wiped the directory).
        # Fall back to the historical ``create_all`` so the app still
        # boots in dev / smoke scenarios.
        from app import models  # noqa: F401

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return

    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sync_engine.url.render_as_string(hide_password=False))
    cfg.attributes["target_metadata"] = Base.metadata
    # Run synchronously inside the thread that ``init_db`` was called
    # from (the FastAPI lifespan context).  Alembic's online mode
    # opens its own connection so this is safe under the running
    # event loop.
    await asyncio.to_thread(command.upgrade, cfg, "head")


async def drop_db() -> None:
    """Drop all tables. Only used in tests."""
    from app import models  # noqa: F401

    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
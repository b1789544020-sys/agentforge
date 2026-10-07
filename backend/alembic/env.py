"""Alembic environment.

We point Alembic at the project's SQLAlchemy ``Base.metadata`` (so
autogenerate sees the same models the app uses) and at the URL the
app is configured to talk to.

The URL is read from the ``AGENTFORGE_DATABASE_URL`` env var (with the
same fallback the app uses: ``backend/agentforge.db``), so a single
``AGENTFORGE_DATABASE_URL=... alembic upgrade head`` works without
editing ``alembic.ini``.

We deliberately *do not* import ``app.database.sync_engine`` here,
because that module eagerly constructs an ``Engine`` at import time
and the resulting ``lru_cache`` on ``get_settings()`` would freeze the
URL before our env override could take effect.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make ``backend/`` importable when ``alembic`` is invoked from any CWD.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.database import Base  # noqa: E402  (sys.path tweak above)

# Import the models package so every ORM class registers with
# ``Base.metadata`` before autogenerate inspects it.
from app import models  # noqa: F401, E402

# Compute the target URL.  We mirror ``app.config.Settings``'s
# ``database_url`` field exactly so the two stay in sync.
DEFAULT_DB = f"sqlite:///{BACKEND_DIR / 'agentforge.db'}"
target_url = os.environ.get("AGENTFORGE_DATABASE_URL", DEFAULT_DB)
# ``AGENTFORGE_DATABASE_URL`` is the async form (``sqlite+aiosqlite:///...``);
# translate it to the sync form for the migration runner.  Strip
# the leading slashes so the Windows drive-letter (``C:/``) is not
# doubled by SQLAlchemy.
if target_url.startswith("sqlite+aiosqlite:///"):
    target_url = "sqlite:///" + target_url[len("sqlite+aiosqlite://") :].lstrip("/")
elif target_url.startswith("postgresql+asyncpg://"):
    target_url = "postgresql+psycopg2://" + target_url[len("postgresql+asyncpg://") :]

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Override the ``sqlalchemy.url`` from .ini with the URL the app is
# actually using.  This keeps a single source of truth (the
# ``AGENTFORGE_DATABASE_URL`` env var) and lets us run migrations
# against the same SQLite file the dev server / tests use.
config.set_main_option("sqlalchemy.url", target_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inherit the app's metadata so autogenerate can compare models
# against the current database schema.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

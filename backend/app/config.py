"""Application configuration.

All runtime configuration is loaded from environment variables (or a .env file).
Use ``get_settings()`` to obtain a cached :class:`Settings` instance.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Strongly-typed application settings."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTFORGE_",
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- App ---------------------------------------------------------
    app_name: str = "AgentForge"
    app_version: str = "0.1.3.dev0"
    environment: Literal["development", "staging", "production", "test"] = "development"
    debug: bool = True
    log_level: str = "INFO"

    # ---- API ---------------------------------------------------------
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
        ]
    )

    # ---- Database ----------------------------------------------------
    # The async URL is the single source of truth; ``sync_database_url``
    # is auto-derived from it (alembic needs the sync form).  We keep
    # ``sync_database_url`` as a regular field with a sentinel default
    # so pydantic-settings still exposes it; the validator rewrites it
    # from ``database_url`` if the user did not set it explicitly.
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR / 'agentforge.db'}"
    sync_database_url: str = f"sqlite:///{BASE_DIR / 'agentforge.db'}"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    @model_validator(mode="after")
    def _derive_sync_url(self) -> "Settings":
        # Always reflect the most recent ``database_url`` so a single
        # env var (``AGENTFORGE_DATABASE_URL``) controls both engines.
        url = self.database_url
        if url.startswith("sqlite+aiosqlite:///"):
            # Strip the async driver prefix and the leading slashes so
            # SQLAlchemy treats the remainder as a normal ``sqlite:///``
            # path.  This avoids the Windows drive-letter doubling
            # (e.g. ``sqlite:////C:/...``) that ``urllib.parse`` would
            # otherwise produce.
            tail = url[len("sqlite+aiosqlite://") :]
            self.sync_database_url = "sqlite:///" + tail.lstrip("/")
        elif url.startswith("postgresql+asyncpg://"):
            self.sync_database_url = "postgresql+psycopg2://" + url[
                len("postgresql+asyncpg://") :
            ]
        return self

    # ---- LLM ---------------------------------------------------------
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_default_model: str = "gpt-4o-mini"

    anthropic_api_key: str | None = None
    anthropic_default_model: str = "claude-3-5-sonnet-20241022"

    llm_provider: Literal["mock", "openai", "anthropic", "deepseek", "qwen"] = "mock"
    request_timeout_seconds: int = 120
    max_retries: int = 3

    # ---- Agent loop --------------------------------------------------
    max_steps: int = 20
    max_context_tokens: int = 8000
    step_timeout_seconds: int = 60

    # ---- Sandbox -----------------------------------------------------
    sandbox_enabled: bool = False
    sandbox_image: str = "python:3.12-slim"
    sandbox_memory_limit: str = "256m"
    sandbox_cpu_quota: float = 1.0
    sandbox_network_disabled: bool = True
    sandbox_timeout_seconds: int = 30

    # ---- Memory ------------------------------------------------------
    memory_max_items: int = 1000
    memory_ttl_seconds: int = 60 * 60 * 24 * 7  # 7 days
    # How often the lifespan-spawned background task runs
    # ``memory.cleanup.cleanup_expired``.  Mirrors the default used by
    # ``app.memory.cleanup.schedule_periodic_cleanup`` (300 s == 5 min).
    # 0 is accepted as a sentinel meaning "do not start the task at
    # all" (useful in tests / one-shot CLIs that want a clean shutdown).
    memory_cleanup_interval_seconds: float = 300.0
    # Vector-memory / embedding settings.  ``vector_backend`` selects the
    # implementation used by ``MemoryStore``: ``"inprocess"`` (default,
    # zero-dep) or ``"chroma"`` (out-of-scope for 0.1.2, requires the
    # ``chromadb`` extra).  ``embedding_dim`` is the dimensionality of
    # the vectors produced by ``app.memory.embeddings.encode`` -- the
    # schema's blob column is variable-width so any dim works but the
    # index size scales with it.
    vector_backend: str = "inprocess"  # "inprocess" | "chroma"
    embedding_dim: int = 128
    embedding_max_tokens: int = 512  # truncate long inputs to keep vectors bounded

    # ---- Auth --------------------------------------------------------
    secret_key: str = "change-me-in-production-please"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24

    # ---- Eval --------------------------------------------------------
    eval_default_dataset: str = "smoke"
    eval_max_concurrency: int = 4

    # ---- Storage -----------------------------------------------------
    storage_dir: Path = BASE_DIR / "storage"
    upload_max_bytes: int = 10 * 1024 * 1024  # 10 MiB

    def ensure_dirs(self) -> None:
        """Create on-disk directories referenced by settings."""
        self.storage_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    settings = Settings()
    settings.ensure_dirs()
    return settings
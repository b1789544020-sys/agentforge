"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.agents import (
    memory_router,
    router as agents_router,
    tools_router,
)
from app.api.evals import router as evals_router
from app.api.runs import router as runs_router
from app.api.tools_metadata import router as tools_metadata_router
from app.api.ws import router as ws_router
from app.config import get_settings
from app.database import AsyncSessionLocal, init_db
from app.memory.cleanup import schedule_periodic_cleanup

logger = logging.getLogger("agentforge")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown hooks.

    In addition to the existing schema bootstrap, we now start the
    memory periodic-cleanup background task so an installed AgentForge
    instance does not depend on somebody calling
    ``schedule_periodic_cleanup`` from a test or a CLI smoke command.
    The task is cancelled at shutdown (FastAPI awaits the generator
    after cancelling all ``asyncio.create_task``-spawned children via
    the standard event-loop teardown -- we still hold a strong
    reference here so it is findable for tests).
    """
    # Re-read settings on every startup so a process that flips the
    # env var between imports (tests, dev reloads) sees the new
    # value without an explicit ``get_settings.cache_clear()`` dance.
    current = get_settings()
    logger.info("Starting %s v%s", current.app_name, current.app_version)
    await init_db()
    logger.info("Database initialised at %s", current.database_url)
    interval = current.memory_cleanup_interval_seconds
    if interval > 0:
        app.state.memory_cleanup_task = schedule_periodic_cleanup(
            AsyncSessionLocal, interval_seconds=interval
        )
        logger.info("Memory periodic cleanup scheduled every %.0fs", interval)
    else:
        app.state.memory_cleanup_task = None
        logger.info("Memory periodic cleanup disabled (interval=0)")
    try:
        yield
    finally:
        task = getattr(app.state, "memory_cleanup_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001
                # ``CancelledError`` is a ``BaseException`` in 3.8+;
                # swallowing it is the happy path (the task was
                # cancelled on purpose).  Any other exception here
                # would already be logged by the loop body.
                pass
        logger.info("Shutting down %s", current.app_name)


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
    )

    # ---- Middleware ----
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- Routers ----
    # Versioned API (e.g. /api/v1/agents)
    app.include_router(agents_router, prefix=settings.api_prefix, tags=["agents"])
    app.include_router(runs_router, prefix=settings.api_prefix, tags=["runs"])
    app.include_router(evals_router, prefix=settings.api_prefix, tags=["evals"])
    app.include_router(memory_router, prefix=settings.api_prefix, tags=["memory"])
    app.include_router(tools_router, prefix=settings.api_prefix, tags=["tools"])
    # 0.1.3 #3 -- tool metadata CRUD endpoints (GET /tools,
    # GET /tools/{name}, PUT /tools/{name}/metadata, DELETE
    # /tools/{name}/metadata).  Mounted under the same prefix as the
    # other routers so the versioned URL mirrors the unversioned one.
    app.include_router(
        tools_metadata_router, prefix=settings.api_prefix, tags=["tools"]
    )
    # WebSocket live-trace stream (0.1.2 #10).  Same router mounted
    # twice so the unversioned ``/api/agents/.../runs/.../ws`` and
    # the versioned ``/api/v1/agents/.../runs/.../ws`` both work,
    # mirroring the HTTP routers above.
    app.include_router(ws_router, prefix=settings.api_prefix, tags=["ws"])
    app.include_router(ws_router, prefix="/api", tags=["ws"])
    # Unversioned aliases (e.g. /api/agents) for client convenience & tests
    app.include_router(agents_router, prefix="/api", tags=["agents"])
    app.include_router(runs_router, prefix="/api", tags=["runs"])
    app.include_router(evals_router, prefix="/api", tags=["evals"])
    app.include_router(memory_router, prefix="/api", tags=["memory"])
    app.include_router(tools_router, prefix="/api", tags=["tools"])
    # 0.1.3 #3 -- unversioned alias for the metadata router so legacy
    # ``/api/tools/{name}/metadata`` URLs work without a ``/v1`` rewrite.
    app.include_router(tools_metadata_router, prefix="/api", tags=["tools"])

    # ---- Health & meta ----
    @app.get("/health", tags=["meta"])
    @app.get("/api/health", tags=["meta"])
    @app.get(f"{settings.api_prefix}/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version}

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
        }

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "type": exc.__class__.__name__},
        )

    return app


app = create_app()

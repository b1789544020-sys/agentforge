"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.auth import require_admin, require_agent_access, require_api_token
from app.api.agents import (
    memory_router,
    router as agents_router,
    tools_router,
)
from app.api.evals import router as evals_router
from app.api.deployments import public_router as delivery_public_router, router as deployments_router
from app.api.gates import router as gates_router
from app.api.knowledge import router as knowledge_router
from app.api.operations import router as operations_router
from app.api.prompts import router as prompts_router
from app.api.runs import router as runs_router
from app.api.teams import router as teams_router
from app.api.templates import router as templates_router
from app.api.tools_metadata import router as tools_metadata_router
from app.api.users import router as users_router
from app.api.ws import router as ws_router
from app.api.ws_broadcaster import broadcaster
from app.config import get_settings
from app.database import AsyncSessionLocal, init_db
from app.memory.cleanup import schedule_periodic_cleanup
from app.observability import (
    configure_logging,
    metrics,
    readiness_payload,
    readiness_response,
    request_context_middleware,
)

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
    if current.run_migrations:
        await init_db()
    logger.info("Database initialised at %s", current.database_url)
    if current.task_backend == "celery" and current.run_recovery_enabled:
        from app.tasks import recover_interrupted_runs

        recovered = await recover_interrupted_runs()
        if recovered:
            logger.info("Requeued %d interrupted run(s)", recovered)
    await broadcaster.start()
    interval = current.memory_cleanup_interval_seconds
    if interval > 0 and current.task_backend != "celery":
        app.state.memory_cleanup_task = schedule_periodic_cleanup(
            AsyncSessionLocal, interval_seconds=interval
        )
        logger.info("Memory periodic cleanup scheduled every %.0fs", interval)
    else:
        app.state.memory_cleanup_task = None
        logger.info("Memory periodic cleanup handled by Celery Beat or disabled")
    try:
        yield
    finally:
        await broadcaster.stop()
        from app.infra.redis import close_redis

        await close_redis()
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
    configure_logging(settings.log_level)
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
    )

    # ---- Middleware ----
    app.middleware("http")(request_context_middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- Routers ----
    protected = [Depends(require_api_token), Depends(require_agent_access)]
    http_routers = (
        (agents_router, "agents"),
        (runs_router, "runs"),
        (evals_router, "evals"),
        (gates_router, "quality-gates"),
        (teams_router, "teams"),
        (operations_router, "operations"),
        (deployments_router, "deployments"),
        (templates_router, "templates"),
        (knowledge_router, "knowledge"),
        (memory_router, "memory"),
        (tools_router, "tools"),
        (tools_metadata_router, "tools"),
        (prompts_router, "prompts"),
    )
    for api_router, tag in http_routers:
        for prefix in (settings.api_prefix, "/api"):
            app.include_router(
                api_router, prefix=prefix, tags=[tag], dependencies=protected
            )
    for prefix in (settings.api_prefix, "/api"):
        app.include_router(users_router, prefix=prefix, tags=["auth"])
    # Public delivery uses a deployment-specific token, never a user JWT.
    app.include_router(delivery_public_router)
    # WebSocket authentication is performed in the handler because browsers
    # use a subprotocol credential rather than an arbitrary HTTP header.
    app.include_router(ws_router, prefix=settings.api_prefix, tags=["ws"])
    app.include_router(ws_router, prefix="/api", tags=["ws"])

    # ---- Health & meta ----
    @app.get("/health", tags=["meta"])
    @app.get("/api/health", tags=["meta"])
    @app.get(f"{settings.api_prefix}/health", tags=["meta"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": settings.app_version,
            "auth_enabled": settings.auth_enabled or bool(settings.api_token),
        }

    @app.get("/health/live", tags=["meta"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["meta"])
    @app.get("/api/health/ready", tags=["meta"])
    @app.get(f"{settings.api_prefix}/health/ready", tags=["meta"])
    async def ready() -> JSONResponse:
        healthy, payload = await readiness_payload()
        return readiness_response(healthy, payload)

    @app.get("/api/metrics", tags=["meta"], dependencies=[Depends(require_api_token), Depends(require_admin)])
    @app.get(f"{settings.api_prefix}/metrics", tags=["meta"], dependencies=[Depends(require_api_token), Depends(require_admin)])
    async def instance_metrics() -> dict[str, Any]:
        broker_depth = None
        if settings.control_backend == "redis":
            try:
                from app.infra.redis import get_redis

                broker_depth = await get_redis().llen("celery")
            except Exception:
                logger.exception("Failed to read Celery queue depth")
        return metrics.snapshot(broker_depth)

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
        }

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled exception: %s", exc, extra={"request_id": request_id})
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
                "type": exc.__class__.__name__,
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id} if request_id else None,
        )

    return app


app = create_app()

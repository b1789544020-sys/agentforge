"""Request IDs, JSON access logs, health probes, and instance metrics."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from starlette.responses import JSONResponse

from app.secrets import redact_text as _redact


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("agentforge")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False


class RequestMetrics:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.instance = f"{socket.gethostname()}:{os.getpid()}"
        self._lock = threading.Lock()
        self._requests = 0
        self._errors = 0
        self._durations: deque[float] = deque(maxlen=10_000)

    def record(self, status_code: int, duration_ms: float) -> None:
        with self._lock:
            self._requests += 1
            self._errors += int(status_code >= 500)
            self._durations.append(duration_ms)

    def snapshot(self, broker_queue_depth: int | None = None) -> dict[str, Any]:
        from app.eval.scheduler import get_eval_scheduler

        with self._lock:
            durations = sorted(self._durations)
            requests = self._requests
            errors = self._errors
        elapsed = max(time.monotonic() - self.started, 0.001)
        p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))] if durations else 0.0
        scheduler = get_eval_scheduler()
        return {
            "instance": self.instance,
            "uptime_seconds": int(elapsed),
            "requests_total": requests,
            "errors_total": errors,
            "error_rate": errors / requests if requests else 0.0,
            "qps": requests / elapsed,
            "duration_ms": {
                "average": sum(durations) / len(durations) if durations else 0.0,
                "p95": p95,
                "samples": len(durations),
            },
            "queue": {
                "eval_active": scheduler.active,
                "eval_queued": scheduler.queued,
                "celery_broker": broker_queue_depth,
            },
        }


metrics = RequestMetrics()
logger = logging.getLogger("agentforge.http")


def _safe_log_path(path: str) -> str:
    if path.startswith("/api/deliver/") and path.endswith("/chat"):
        return "/api/deliver/[redacted]/chat"
    return path


async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id", "").strip()[:64] or uuid.uuid4().hex
    request.state.request_id = request_id
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration = (time.perf_counter() - start) * 1000
        metrics.record(500, duration)
        logger.exception(
            "request failed",
            extra={"request_id": request_id},
        )
        raise
    duration = (time.perf_counter() - start) * 1000
    metrics.record(response.status_code, duration)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "%s %s -> %s (%.2fms)",
        request.method,
        _safe_log_path(request.url.path),
        response.status_code,
        duration,
        extra={"request_id": request_id},
    )
    return response


async def readiness_payload() -> tuple[bool, dict[str, Any]]:
    from sqlalchemy import text

    from app.config import get_settings
    from app.database import async_session_factory

    settings = get_settings()
    components: dict[str, str] = {}
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        components["database"] = "ok"
    except Exception:
        components["database"] = "error"
    if settings.control_backend == "redis":
        try:
            from app.infra.redis import get_redis

            await get_redis().ping()
            components["redis"] = "ok"
        except Exception:
            components["redis"] = "error"
    if settings.vector_backend == "qdrant":
        try:
            import asyncio
            from qdrant_client import QdrantClient

            client = QdrantClient(url=settings.vector_url, api_key=settings.vector_api_key)
            await asyncio.to_thread(client.get_collections)
            await asyncio.to_thread(client.close)
            components["qdrant"] = "ok"
        except Exception:
            components["qdrant"] = "error"
    healthy = all(value == "ok" for value in components.values())
    return healthy, {"status": "ok" if healthy else "degraded", "components": components}


def readiness_response(healthy: bool, payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=200 if healthy else 503, content=payload)


__all__ = [
    "JsonFormatter", "configure_logging", "metrics", "request_context_middleware",
    "readiness_payload", "readiness_response",
]

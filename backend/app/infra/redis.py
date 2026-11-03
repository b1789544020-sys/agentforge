"""Lazy Redis connection shared by controls, events, and task helpers."""
from __future__ import annotations

from typing import Any

_client: Any = None


def get_redis() -> Any:
    global _client
    if _client is None:
        from redis.asyncio import Redis
        from app.config import get_settings

        _client = Redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


__all__ = ["get_redis", "close_redis"]

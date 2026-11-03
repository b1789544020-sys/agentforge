"""Cancellation signals for evaluation runs (separate from Agent runs)."""
from __future__ import annotations

import asyncio


_events: dict[str, asyncio.Event] = {}


def register(eval_run_id: str) -> asyncio.Event:
    event = _events.get(eval_run_id)
    if event is None:
        event = asyncio.Event()
        _events[eval_run_id] = event
    return event


def request_cancel(eval_run_id: str) -> bool:
    event = _events.get(eval_run_id)
    if event is None:
        return False
    event.set()
    return True


def unregister(eval_run_id: str) -> None:
    _events.pop(eval_run_id, None)


def _cancel_key(eval_run_id: str) -> str:
    return f"agentforge:control:eval-cancel:{eval_run_id}"


async def request_cancel_async(eval_run_id: str) -> bool:
    local = request_cancel(eval_run_id)
    from app.config import get_settings

    if get_settings().control_backend != "redis":
        return local
    from app.infra.redis import get_redis

    await get_redis().set(_cancel_key(eval_run_id), "1", ex=24 * 3600)
    return True


async def cancel_requested(eval_run_id: str, event: asyncio.Event) -> bool:
    if event.is_set():
        return True
    from app.config import get_settings

    if get_settings().control_backend != "redis":
        return False
    from app.infra.redis import get_redis

    return bool(await get_redis().get(_cancel_key(eval_run_id)))


async def wait_for_cancel(eval_run_id: str, event: asyncio.Event) -> None:
    while not await cancel_requested(eval_run_id, event):
        await asyncio.sleep(0.1)


async def unregister_async(eval_run_id: str) -> None:
    unregister(eval_run_id)
    from app.config import get_settings

    if get_settings().control_backend == "redis":
        from app.infra.redis import get_redis

        await get_redis().delete(_cancel_key(eval_run_id))


__all__ = [
    "register", "request_cancel", "unregister", "request_cancel_async",
    "cancel_requested", "wait_for_cancel", "unregister_async",
]

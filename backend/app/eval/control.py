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


__all__ = ["register", "request_cancel", "unregister"]

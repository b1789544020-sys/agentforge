"""Lifespan / startup-shutdown hook tests.

The FastAPI ``lifespan`` in :mod:`app.main` is responsible for two
things at boot:

1. Running the existing schema bootstrap (``alembic upgrade head``).
2. Spawning the periodic memory cleanup task via
   :func:`app.memory.cleanup.schedule_periodic_cleanup`.

These tests cover the second part: when the app starts the task is
running and cancellable, and when ``AGENTFORGE_MEMORY_CLEANUP_INTERVAL_SECONDS=0``
is set the task is suppressed entirely (so one-shot CLIs / dev loops
do not leak background workers).
"""
from __future__ import annotations

import asyncio

import pytest

from app.main import create_app


@pytest.mark.asyncio
async def test_lifespan_starts_memory_cleanup_task() -> None:
    """Default config: the periodic cleanup task is created on startup
    and is cancelled on shutdown."""
    app = create_app()
    # Drive the lifespan manually -- the FastAPI TestClient already
    # exercises this, but a direct call keeps the assertions
    # explicit and the failure messages readable.
    cm = app.router.lifespan_context(app)
    await cm.__aenter__()
    try:
        task = getattr(app.state, "memory_cleanup_task", None)
        assert task is not None, "lifespan must spawn the memory cleanup task"
        assert not task.done(), "cleanup task should be running while app is up"
    finally:
        await cm.__aexit__(None, None, None)
    # After shutdown the task should be cancelled (it may already be
    # done or in the process of finishing).
    task = getattr(app.state, "memory_cleanup_task", None)
    assert task is not None
    # Give the event loop a tick to settle the cancellation.
    for _ in range(20):
        if task.done():
            break
        await asyncio.sleep(0.01)
    assert task.done(), "cleanup task should be cancelled on shutdown"


@pytest.mark.asyncio
async def test_lifespan_zero_interval_disables_cleanup(monkeypatch) -> None:
    """``AGENTFORGE_MEMORY_CLEANUP_INTERVAL_SECONDS=0`` disables the
    background task entirely (no spawn, no leak)."""
    monkeypatch.setenv("AGENTFORGE_MEMORY_CLEANUP_INTERVAL_SECONDS", "0")
    from app import config as config_mod

    config_mod.get_settings.cache_clear()
    try:
        app = create_app()
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        try:
            task = getattr(app.state, "memory_cleanup_task", None)
            assert task is None, "interval=0 should suppress the cleanup task"
        finally:
            await cm.__aexit__(None, None, None)
    finally:
        config_mod.get_settings.cache_clear()
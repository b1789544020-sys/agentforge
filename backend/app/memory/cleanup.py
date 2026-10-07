"""Periodic background tasks for the memory subsystem.

Exposes :func:`schedule_periodic_cleanup` -- a thin wrapper around
``asyncio.create_task`` that runs ``cleanup_expired`` (defined in
``app.agent.memory``) on a fixed interval until the application
shuts down.

The task is intentionally cooperative: any exception in a single
cleanup cycle is logged and swallowed so a transient DB error does
not kill the loop.  We rely on the FastAPI lifespan (or equivalent
process supervisor) to cancel the task at shutdown; the test suite
exercises this with a direct ``task.cancel()``.

Note: ``cleanup_expired`` is imported lazily to avoid a circular
import between :mod:`app.memory.cleanup` and :mod:`app.agent.memory`
(the agent module already imports :mod:`app.memory`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# A session factory returns a fresh AsyncSession each call.  Using a
# factory instead of a single session keeps the cleanup loop from
# holding a long-lived transaction open.
SessionFactory = Callable[[], "AsyncSession | Awaitable[AsyncSession]"]


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


async def _cleanup_once(
    session_factory: SessionFactory, *, agent_id: Optional[str] = None
) -> int:
    from app.agent.memory import cleanup_expired

    session = await _maybe_await(session_factory())
    try:
        return await cleanup_expired(session, agent_id=agent_id)
    finally:
        close = getattr(session, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


async def _cleanup_loop(
    session_factory: SessionFactory,
    *,
    interval_seconds: float,
    agent_id: Optional[str] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Inner loop; broken out so tests can drive it directly."""
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            deleted = await _cleanup_once(session_factory, agent_id=agent_id)
            if deleted:
                logger.info("memory cleanup deleted %d expired rows", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("memory cleanup cycle failed")
        # Sleep with early-exit on stop_event.
        if stop_event is None:
            await asyncio.sleep(interval_seconds)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass
            else:
                return


def schedule_periodic_cleanup(
    session_factory: SessionFactory,
    *,
    interval_seconds: float = 300.0,
    agent_id: Optional[str] = None,
) -> asyncio.Task:
    """Schedule a background task that runs ``cleanup_expired`` forever.

    Returns the :class:`asyncio.Task` so the caller can cancel it
    during shutdown.  ``interval_seconds`` defaults to 5 minutes --
    small enough that a typical 1-hour TTL row sticks around for at
    most one extra cycle, large enough that the DB is not hammered.
    """
    return asyncio.create_task(
        _cleanup_loop(
            session_factory,
            interval_seconds=interval_seconds,
            agent_id=agent_id,
        ),
        name="memory.periodic_cleanup",
    )


__all__ = [
    "SessionFactory",
    "schedule_periodic_cleanup",
]

"""WebSocket endpoint that streams a run's trace live.

The :func:`stream_run` handler in this module is the consumer side of
:mod:`app.api.ws_broadcaster`.  The producer side lives in
:mod:`app.api.runs` (see ``_execute_run`` / ``_resume_run``) which
injects a ``LoopConfig(on_step=...)`` callback into the
:class:`AgentLoop` so every step is fanned out to subscribers
*as soon as it is committed to the database*.

Message protocol
----------------
All messages are JSON objects with a string ``event`` field.  The
server pushes four shapes; the client never needs to send anything
(text frames from the client are accepted and ignored so the same
socket can be used for ping/pong in the future).

``run.snapshot``
    Sent once on connect.  Carries the current ``run`` payload
    (output / status / token counts) plus its full ``trace`` so a
    client connecting mid-run or after a reconnect has the
    historical steps immediately.

``run.step``
    Emitted once per step the agent loop commits.  ``payload``
    mirrors the wire shape of :func:`app.api.runs._serialize_step`
    so the front-end can append it to the existing trace list
    without a re-mapping pass.

``run.complete``
    Emitted once when the loop finishes, regardless of outcome
    (success / failed / cancelled / paused / terminated).  Carries
    the final ``output``, ``status``, and token counts so the
    front-end can stop polling and update the history list.

``run.error``
    Emitted when something goes wrong *before* the loop is even
    constructed (e.g. agent not found).  After ``run.error`` the
    socket is closed.

Backpressure
------------
The broadcaster drops messages for slow consumers rather than
blocking the agent loop.  When the WebSocket handler detects that
its queue was pruned (slow client) it closes the socket cleanly.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.api.runs import _serialize_run
from app.api.ws_broadcaster import broadcaster
from app.database import async_session_factory
from app.models import Run, Step

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])


# Statuses considered terminal.  Matches the values written by
# ``AgentLoop`` and ``_execute_run``; not every value is a valid
# ``Run.status`` (e.g. ``ok`` is a step status) but the intersect
# is small and explicit is better than magical.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"success", "failed", "cancelled", "terminated", "completed"}
)


# 0.1.2 #10 WebSocket — /api/agents/{id}/runs/{id}/ws
# The path is mounted both with and without the /api/v1 prefix in
# ``app.main`` so curl-based tests (which hit the unversioned
# ``/api`` alias) and a future v2 client can both subscribe without
# any client-side branching.
@router.websocket("/agents/{agent_id}/runs/{run_id}/ws")
async def stream_run(websocket: WebSocket, agent_id: str, run_id: str) -> None:
    """Push every step of ``run_id`` to the client as it is produced.

    On accept we send a snapshot of the run plus its full existing
    trace (so a client connecting mid-run or after a reconnect gets
    the historical steps immediately) and then start streaming live
    updates.  The socket stays open until one of:

    * the loop ends and we emit ``run.complete``;
    * the client disconnects (``WebSocketDisconnect``);
    * the agent loop raises and we emit ``run.error``.
    """
    # Validate the run belongs to the agent *before* accepting so a
    # bad URL gives a clean 403-ish close rather than a hung socket.
    async with async_session_factory() as session:
        run = await session.get(Run, run_id)
        if run is None or run.agent_id != agent_id:
            await websocket.close(code=4404, reason="run not found")
            return

    await websocket.accept()

    # Push a snapshot of what we already know.  Steps are ordered by
    # index so the front-end can append them straight onto the
    # trace list with no re-sorting.
    async with async_session_factory() as session:
        run = await session.get(Run, run_id)
        steps = (
            await session.execute(
                select(Step).where(Step.run_id == run_id).order_by(Step.index.asc())
            )
        ).scalars().all()
        snapshot = {
            "event": "run.snapshot",
            "run": _serialize_run(run, steps),
        }
    await _send(websocket, snapshot)

    # If the run already terminated before the client connected
    # there's nothing left to stream; emit ``run.complete`` once
    # so the client can drop into its terminal UI.
    if run is not None and run.status in _TERMINAL_STATUSES:
        await _send(
            websocket,
            {
                "event": "run.complete",
                "run_id": run_id,
                "agent_id": agent_id,
                "status": run.status,
                "output": run.output,
                "total_tokens": run.total_tokens,
                "duration_ms": run.duration_ms,
                "step_count": run.step_count,
                "error": run.error,
            },
        )
        await websocket.close()
        return

    queue = await broadcaster.subscribe(run_id)
    drop_pruned = False

    async def drain_broadcaster() -> None:
        """Forward every queued broadcast to the WebSocket as JSON."""
        nonlocal drop_pruned
        while True:
            try:
                message = await queue.get()
            except asyncio.CancelledError:
                raise
            if message.get("event") == "__dropped__":
                drop_pruned = True
                return
            try:
                await _send(websocket, message)
            except (WebSocketDisconnect, RuntimeError):
                return

    drain_task = asyncio.create_task(drain_broadcaster())

    # We also need to notice if the *client* goes away (close /
    # network drop).  ``websocket.receive_text()`` raises
    # ``WebSocketDisconnect`` in that case; we swallow it and let
    # the finally clause clean up.
    try:
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        drain_task.cancel()
        try:
            await drain_task
        except (asyncio.CancelledError, Exception):
            pass
        await broadcaster.unsubscribe(run_id, queue)
        if drop_pruned:
            try:
                await websocket.close(code=1013, reason="client too slow")
            except RuntimeError:
                pass


async def _send(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Send ``payload`` as a JSON text frame on ``websocket``.

    Centralised so the tests can monkeypatch a single function
    rather than the websocket object itself.
    """
    await websocket.send_text(json.dumps(payload, default=str))


__all__ = ["router", "stream_run", "_send"]

"""Process-local WebSocket fan-out for in-flight :class:`Run` executions.

While an :class:`AgentLoop` is running, every step it produces is
broadcast to all connected WebSocket subscribers for that ``run_id``
so the front-end can show the trace live.  Subscribers come and go
(opening a WebSocket counts as ``subscribe``; disconnect counts as
``unsubscribe``); the loop never blocks on slow clients.

Why process-local and not Redis pub/sub?
----------------------------------------
AgentForge is a single-process FastAPI app.  Out-of-process pub/sub
would be premature infrastructure.  If/when the backend gets sharded
horizontally, the broadcaster should be replaced with a Redis
channel or NATS subject that mirrors the same API surface.

This module is intentionally a tiny singleton; the design contract
is:

* :func:`RunBroadcaster.subscribe` returns a fresh
  :class:`asyncio.Queue` and registers it in the ``run_id`` set; the
  caller is responsible for draining it (the WebSocket handler does
  so in a loop).
* :func:`RunBroadcaster.publish_step` and
  :func:`RunBroadcaster.publish_complete` are non-blocking: they
  ``put_nowait`` into every subscriber queue and silently drop the
  message on a full queue.  Slow clients must not stall the agent
  loop.
* :func:`RunBroadcaster.unsubscribe` is idempotent; the broadcaster
  prunes empty sets so a long-lived process does not leak memory
  across many runs.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


class _RunChannel:
    """The set of subscribers currently listening to one ``run_id``."""

    __slots__ = ("subscribers",)

    def __init__(self) -> None:
        # A list (not a set) so the WebSocket handler can iterate
        # without surprises; the broadcaster appends / removes under
        # a lock so iteration is safe.
        self.subscribers: list[asyncio.Queue[dict[str, Any]]] = []


class RunBroadcaster:
    """Process-local pub/sub for ``run_id`` step events."""

    def __init__(self) -> None:
        # run_id -> _RunChannel.  Plain dict guarded by a single lock;
        # operations are O(1) and the critical section is microseconds
        # so contention is not a concern for a FastAPI process.
        self._channels: dict[str, _RunChannel] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        run_id: str,
        *,
        maxsize: int = 256,
    ) -> asyncio.Queue[dict[str, Any]]:
        """Return a new queue subscribed to ``run_id`` events.

        The caller (typically a WebSocket handler) is expected to
        drain the queue and call :func:`unsubscribe` on disconnect.
        ``maxsize`` defaults to 256; tests pass a smaller value
        to exercise the slow-consumer / drop branch deterministically.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        async with self._lock:
            channel = self._channels.get(run_id)
            if channel is None:
                channel = _RunChannel()
                self._channels[run_id] = channel
            channel.subscribers.append(queue)
        logger.debug(
            "ws subscribe run_id=%s subscribers=%d",
            run_id,
            len(channel.subscribers),
        )
        return queue

    async def unsubscribe(
        self, run_id: str, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        """Remove ``queue`` from the ``run_id`` channel.  Idempotent."""
        async with self._lock:
            channel = self._channels.get(run_id)
            if channel is None:
                return
            try:
                channel.subscribers.remove(queue)
            except ValueError:
                return
            if not channel.subscribers:
                # No more listeners -> drop the channel so the dict
                # does not grow without bound across thousands of runs.
                del self._channels[run_id]
                logger.debug("ws channel pruned run_id=%s", run_id)

    def publish_step(self, run_id: str, payload: dict[str, Any]) -> None:
        """Fan a ``run.step`` event out to every subscriber.

        Non-blocking: full queues are dropped (and logged at INFO
        level once) so a slow client cannot stall the agent loop.
        """
        self._fanout(run_id, payload, event="run.step")

    def publish_complete(self, run_id: str, payload: dict[str, Any]) -> None:
        """Fan a terminal event out (caller picks the event name).

        Same non-blocking guarantee as :func:`publish_step`.  Used
        for both ``run.complete`` (success / cancelled / paused /
        terminated) and ``run.error`` shapes -- the broadcaster stays
        payload-only so both fit through the same call.
        """
        self._fanout(run_id, payload, event=None)

    def _fanout(
        self,
        run_id: str,
        payload: dict[str, Any],
        event: Optional[str],
    ) -> None:
        # Snapshot the subscriber list without holding the lock;
        # the worst case is a benign iteration on a stale list
        # (the queue is then unsubscribed on the next ``unsubscribe``
        # call and discarded).
        channel = self._channels.get(run_id)
        if channel is None or not channel.subscribers:
            return
        message = dict(payload) if event is None else {"event": event, **payload}
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in list(channel.subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Slow consumer.  Mark it dead so we stop pushing,
                # then poke a sentinel so the handler's ``queue.get()``
                # unblocks and the socket can close.  The handler is
                # expected to drop the sentinel silently and shut
                # down its send loop.
                dead.append(q)
                logger.info(
                    "ws subscriber queue full; dropping run_id=%s",
                    run_id,
                )
        for q in dead:
            try:
                # Drain one slot to make room for the sentinel.
                # If the consumer happens to be reading between
                # our last ``put_nowait`` and this ``get_nowait``
                # we may pop a *real* event -- that's fine, the
                # consumer will discard the entire channel anyway
                # when it sees the sentinel.
                q.get_nowait()
            except (asyncio.QueueEmpty, ValueError):
                pass
            try:
                q.put_nowait({"event": "__dropped__"})
            except asyncio.QueueFull:
                # Both slots full (shouldn't happen after the
                # drain above unless another coroutine filled it
                # again).  Skip the sentinel; the channel removal
                # below still wins and the consumer will block
                # until its own disconnect.
                pass
            try:
                channel.subscribers.remove(q)
            except ValueError:
                pass
        if not channel.subscribers:
            self._channels.pop(run_id, None)

    # ------------------------------------------------------------------
    # Introspection helpers (used by tests + the WebSocket handler)
    # ------------------------------------------------------------------

    def subscriber_count(self, run_id: str) -> int:
        channel = self._channels.get(run_id)
        return 0 if channel is None else len(channel.subscribers)

    def active_runs(self) -> Iterable[str]:
        return list(self._channels.keys())


# Module-level singleton.  Importing modules grab this and the
# FastAPI app process shares it.
broadcaster = RunBroadcaster()


__all__ = ["RunBroadcaster", "broadcaster"]


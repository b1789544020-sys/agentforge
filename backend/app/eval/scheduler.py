"""Fair, process-local evaluation queue with a global concurrency bound."""
from __future__ import annotations

import asyncio
import heapq
from contextlib import asynccontextmanager
from typing import AsyncIterator


class EvalScheduler:
    """FIFO within a priority; lower numeric priority values run first."""

    def __init__(self, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.max_concurrency = max_concurrency
        self._condition = asyncio.Condition()
        self._waiters: list[tuple[int, int]] = []
        self._sequence = 0
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def queued(self) -> int:
        return len(self._waiters)

    @asynccontextmanager
    async def slot(self, *, priority: int = 0) -> AsyncIterator[None]:
        ticket = (priority, self._sequence)
        self._sequence += 1
        async with self._condition:
            heapq.heappush(self._waiters, ticket)
            try:
                while self._active >= self.max_concurrency or self._waiters[0] != ticket:
                    await self._condition.wait()
            except BaseException:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                    heapq.heapify(self._waiters)
                    self._condition.notify_all()
                raise
            heapq.heappop(self._waiters)
            self._active += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active -= 1
                self._condition.notify_all()


_scheduler: EvalScheduler | None = None


def get_eval_scheduler() -> EvalScheduler:
    global _scheduler
    if _scheduler is None:
        from app.config import get_settings

        _scheduler = EvalScheduler(get_settings().eval_max_concurrency)
    return _scheduler


def _reset_eval_scheduler() -> None:
    """Test hook for replacing the process-local queue."""
    global _scheduler
    _scheduler = None


__all__ = ["EvalScheduler", "get_eval_scheduler", "_reset_eval_scheduler"]

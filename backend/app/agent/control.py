"""Process-local control signals for in-flight :class:`AgentLoop` runs.

This module lets the API layer reach an AgentLoop that's currently
executing (e.g. on a background task) so the user can issue a cooperative
``pause()`` or ``resume_signal()`` from another HTTP request.

Why process-local and not a DB column?
-------------------------------------
* Setting/clearing an ``asyncio.Event`` is essentially free.
* Polling the DB on every loop step would add one extra ``SELECT`` per
  step (latency + lock pressure).
* If the process restarts while a run is paused, the persisted
  ``Run.state`` column already captures everything we need to rebuild
  the loop via :meth:`AgentLoop.resume`, so on-the-fly signals are not
  required for correctness — only for live control.

The :data:`_LOOP_REGISTRY` mapping is intentionally a plain ``dict``.
It is populated by :meth:`AgentLoop.__init__` and is only consulted
from the FastAPI request handler thread, both of which run in the
same asyncio loop, so a lock is unnecessary.

A separate :data:`_PAUSE_SIGNALS` mapping holds the asyncio.Event
each AgentLoop is using for cooperative pause.  The API layer sets
the Event (to request a pause) and clears it (to clear an
already-pending pause request).  When the loop finishes, the entry is
removed so the dict does not grow without bound.
"""
from __future__ import annotations

import asyncio
from typing import Dict, Optional

# run_id -> AgentLoop instance
_LOOP_REGISTRY: Dict[str, "object"] = {}

# run_id -> the asyncio.Event AgentLoop uses to check for pause requests
# (kept here so the API can ``.set()`` / ``.clear()`` it from another
# request without holding a reference to the loop).
_PAUSE_SIGNALS: Dict[str, asyncio.Event] = {}


def register_run(run_id: str, loop: "object", pause_signal: asyncio.Event) -> None:
    """Bind ``run_id`` to the in-flight :class:`AgentLoop` and its pause Event."""
    _LOOP_REGISTRY[run_id] = loop
    _PAUSE_SIGNALS[run_id] = pause_signal


def unregister_run(run_id: str) -> None:
    """Drop a finished run from the registry.  Safe to call multiple times."""
    _LOOP_REGISTRY.pop(run_id, None)
    _PAUSE_SIGNALS.pop(run_id, None)


def get_loop(run_id: str) -> Optional["object"]:
    """Return the in-flight :class:`AgentLoop` for ``run_id`` (or ``None``)."""
    return _LOOP_REGISTRY.get(run_id)


def request_pause(run_id: str) -> bool:
    """Ask the in-flight loop to pause at its next checkpoint.

    Returns ``True`` if a live loop was found and signalled, ``False``
    otherwise (i.e. the run is not in this process — likely already
    finished or never started here).
    """
    ev = _PAUSE_SIGNALS.get(run_id)
    if ev is None:
        return False
    ev.set()
    return True


def clear_pause(run_id: str) -> bool:
    """Cancel a previously-requested pause (used when resuming a paused run)."""
    ev = _PAUSE_SIGNALS.get(run_id)
    if ev is None:
        return False
    ev.clear()
    return True


__all__ = [
    "register_run",
    "unregister_run",
    "get_loop",
    "request_pause",
    "clear_pause",
]

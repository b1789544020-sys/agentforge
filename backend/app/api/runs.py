"""Run / trace endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import control
from app.agent.loop import AgentLoop, LoopConfig, LoopResult
from app.api.schemas import RunCreate, RunOut, RunSummary
from app.api.ws_broadcaster import broadcaster
from app.database import async_session_factory, get_session
from app.models import Agent, Run, Step, RUN_STATUS_PAUSED, RUN_STATUS_RUNNING

router = APIRouter(prefix="/agents/{agent_id}/runs", tags=["runs"])

logger = logging.getLogger(__name__)


def _build_on_step(run_id: str) -> Any:
    """Build a ``LoopConfig.on_step`` callback that fans out a step.

    The callback is invoked by :class:`AgentLoop` *after* the step
    is committed to the database (see ``AgentLoop._step_once``),
    which means the WebSocket consumer can read the canonical row
    via the existing ``_serialize_step`` shape.  Any exception the
    broadcaster might raise is logged and swallowed so a slow or
    broken WebSocket client can never poison the agent loop.
    """

    def _on_step(step: Step) -> None:
        try:
            broadcaster.publish_step(run_id, _serialize_step(step))
        except Exception:  # noqa: BLE001 - never break the loop
            logger.exception("ws publish_step failed run_id=%s", run_id)

    return _on_step


def _publish_complete(run_id: str, run: Run) -> None:
    """Push the terminal ``run.complete`` event to every subscriber."""
    try:
        broadcaster.publish_complete(
            run_id,
            {
                "event": "run.complete",
                "run_id": run_id,
                "agent_id": run.agent_id,
                "status": run.status,
                "output": run.output,
                "total_tokens": run.total_tokens,
                "duration_ms": run.duration_ms,
                "step_count": run.step_count,
                "error": run.error,
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("ws publish_complete failed run_id=%s", run_id)



def _serialize_step(step: Step) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if step.payload:
        try:
            payload = json.loads(step.payload)
        except (TypeError, ValueError):
            payload = {"raw": step.payload}
    # 0.1.3 #9 — risk badge.  Only meaningful for ``tool_call`` steps;
    # the agent loop writes ``payload["risk"]`` from
    # ``ToolRegistry.spec(name).risk`` at step-record time.  Surfaced
    # as a top-level field for the frontend so the trace UI doesn't
    # have to know about the payload shape.
    risk_value: str | None = None
    if step.kind == "tool_call" and isinstance(payload, dict):
        v = payload.get("risk")
        if isinstance(v, str) and v:
            risk_value = v
    return {
        "id": step.id,
        "index": step.index,
        "kind": step.kind,
        "name": step.name,
        "content": step.content,
        "payload": payload,
        "duration_ms": step.duration_ms,
        "tokens": step.tokens,
        "status": step.status,
        "error": step.error,
        "created_at": step.created_at,
        "risk": risk_value,
    }


def _serialize_run(run: Run, steps: list[Step] | None = None) -> dict[str, Any]:
    trace: list[dict[str, Any]] = []
    for s in steps or []:
        trace.append(_serialize_step(s))
    return {
        "id": run.id,
        "agent_id": run.agent_id,
        "input": run.input,
        "output": run.output,
        "status": run.status,
        "error": run.error,
        "total_tokens": run.total_tokens,
        "prompt_tokens": run.prompt_tokens,
        "completion_tokens": run.completion_tokens,
        "duration_ms": run.duration_ms,
        "step_count": run.step_count,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "trace": trace,
    }


@router.get("", response_model=list[RunSummary])
async def list_runs(
    agent_id: str,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Run)
            .where(Run.agent_id == agent_id)
            .order_by(Run.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "agent_id": r.agent_id,
            "input": r.input,
            "output": r.output,
            "status": r.status,
            "total_tokens": r.total_tokens,
            "duration_ms": r.duration_ms,
            "step_count": r.step_count,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
        }
        for r in rows
    ]


@router.post("", response_model=RunOut)
async def create_run(
    agent_id: str,
    payload: RunCreate,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")

    # Apply per-run overrides without mutating the agent record.
    if payload.model:
        agent.model = payload.model
    if payload.temperature is not None:
        agent.temperature = payload.temperature
    if payload.max_steps is not None:
        agent.max_steps = payload.max_steps

    run = Run(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        input=payload.input,
        status="running",
        started_at=datetime.utcnow(),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    if payload.async_run:
        background.add_task(_execute_run, run.id, agent_id, payload.input, payload.session_id)
        return _serialize_run(run, [])

    result = await _execute_run(run.id, agent_id, payload.input, payload.session_id)
    return result


@router.get("/{run_id}", response_model=RunOut)
async def get_run(
    agent_id: str,
    run_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = await session.get(Run, run_id)
    if not run or run.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="run not found")
    steps = (
        await session.execute(select(Step).where(Step.run_id == run_id).order_by(Step.index.asc()))
    ).scalars().all()
    return _serialize_run(run, steps)


@router.delete("/{run_id}")
async def delete_run(
    agent_id: str,
    run_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    run = await session.get(Run, run_id)
    if not run or run.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="run not found")
    await session.delete(run)
    await session.commit()
    return {"ok": True, "deleted_id": run_id}


async def _execute_run(
    run_id: str, agent_id: str, user_input: str, session_id: str | None
) -> dict[str, Any]:
    """Execute the agent loop and persist trace + result. Returns serialized run.

    The in-flight :class:`AgentLoop` is registered with
    :mod:`app.agent.control` so the API layer can ask it to pause via
    :func:`control.request_pause`.  The entry is removed in ``finally``
    so the process-local registry never grows without bound.
    """
    from app.database import async_session_factory  # local import to avoid cycle

    async with async_session_factory() as session:
        agent = await session.get(Agent, agent_id)
        run = await session.get(Run, run_id)
        if not agent or not run:
            return {}
        # 0.1.2 #10 WebSocket: wire the broadcaster into the loop
        # via LoopConfig.on_step so every committed step fans out
        # to subscribed WebSocket clients in real time.
        loop = AgentLoop(
            agent,
            session,
            session_id=session_id,
            config=LoopConfig(on_step=_build_on_step(run_id)),
        )
        # Register BEFORE we await so the API can flip the pause flag
        # even on the very first step of the loop.
        control.register_run(run_id, loop, loop._pause)
        try:
            result: LoopResult = await loop.run(user_input, run_id=run_id)
        finally:
            control.unregister_run(run_id)
        run.output = result.output
        run.status = result.status
        run.error = result.error
        run.total_tokens = result.total_tokens
        run.prompt_tokens = result.prompt_tokens
        run.completion_tokens = result.completion_tokens
        run.duration_ms = result.duration_ms
        run.step_count = result.step_count
        run.finished_at = datetime.utcnow()
        await session.commit()
        # Push the terminal event AFTER the row is finalised so a
        # late-arriving client that immediately polls the run row
        # will see the same shape that the ws frame advertised.
        _publish_complete(run_id, run)
        steps = (
            await session.execute(
                select(Step).where(Step.run_id == run_id).order_by(Step.index.asc())
            )
        ).scalars().all()
        return _serialize_run(run, steps)


# ---------------------------------------------------------------------------
# Cooperative pause / resume
# ---------------------------------------------------------------------------
@router.post("/{run_id}/pause", response_model=RunOut)
async def pause_run(
    agent_id: str,
    run_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Cooperatively ask a running loop to pause at its next checkpoint.

    Two outcomes are possible:

    * The loop is alive **in this process** — the asyncio.Event is set
      directly and the loop will write its :class:`RunState` into
      ``Run.state`` and flip ``status`` to ``paused`` at the next
      step.  This response returns the *current* run row, which is
      still ``running`` until the loop actually stops.
    * The loop is not in this process (already finished, paused, or
      hosted in a different worker) — the DB row is read and returned
      as-is so the caller can see what state the run is in.
    """
    run = await session.get(Run, run_id)
    if not run or run.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="run not found")

    if run.status == RUN_STATUS_PAUSED:
        # Already paused; nothing to do but return the current state.
        steps = (
            await session.execute(
                select(Step).where(Step.run_id == run_id).order_by(Step.index.asc())
            )
        ).scalars().all()
        return _serialize_run(run, steps)

    if run.status not in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot pause run in status {run.status!r}",
        )

    signalled = control.request_pause(run_id)
    if not signalled:
        # Loop is not in this process — the run may be hosted on
        # another worker or already be in a terminal state.  We still
        # return 200 because there's nothing wrong with the request;
        # the caller can poll the run status separately.
        logger.info(
            "pause requested for run %s but no live loop found locally", run_id
        )

    # Return whatever the DB currently shows — the row will be updated
    # by the loop itself when it reaches the next checkpoint.
    steps = (
        await session.execute(
            select(Step).where(Step.run_id == run_id).order_by(Step.index.asc())
        )
    ).scalars().all()
    return _serialize_run(run, steps)


@router.post("/{run_id}/resume", response_model=RunOut)
async def resume_run(
    agent_id: str,
    run_id: str,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Resume a paused run.

    Rebuilds the loop from the persisted :class:`RunState` (stored in
    ``Run.state``) and runs it as a background task.  The cleared
    pause flag is propagated to any future loop instance that may
    already be in-flight for the same run.
    """
    run = await session.get(Run, run_id)
    if not run or run.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status != RUN_STATUS_PAUSED:
        raise HTTPException(
            status_code=409,
            detail=f"run is not paused (status={run.status!r})",
        )
    if not run.state:
        raise HTTPException(
            status_code=409,
            detail="paused run has no persisted state — cannot resume",
        )

    # Optimistically flip the status so the UI shows "running" while
    # the background task spins up.  The loop's first commit will
    # reaffirm this.
    run.status = RUN_STATUS_RUNNING
    run.error = None
    await session.commit()
    await session.refresh(run)

    # In case the loop is also still alive in this process (e.g. the
    # user clicked pause then resume very quickly), clear any
    # outstanding pause request so the next checkpoint isn't paused
    # again immediately.
    control.clear_pause(run_id)

    background.add_task(
        _resume_run, run_id, agent_id, run.session_id, run.input
    )

    steps = (
        await session.execute(
            select(Step).where(Step.run_id == run_id).order_by(Step.index.asc())
        )
    ).scalars().all()
    return _serialize_run(run, steps)


async def _resume_run(
    run_id: str, agent_id: str, session_id: str | None, user_input: str
) -> dict[str, Any]:
    """Background task: rebuild the loop from ``Run.state`` and keep going."""
    async with async_session_factory() as session:
        agent = await session.get(Agent, agent_id)
        run = await session.get(Run, run_id)
        if not agent or not run:
            return {}
        # 0.1.2 #10 WebSocket: same on_step fan-out as ``_execute_run``
        # so resume picks up live updates from the very first step.
        loop = AgentLoop(
            agent,
            session,
            session_id=session_id,
            config=LoopConfig(on_step=_build_on_step(run_id)),
        )
        control.register_run(run_id, loop, loop._pause)
        try:
            result: LoopResult = await loop.resume(run_id)
        finally:
            control.unregister_run(run_id)
        run.output = result.output
        run.status = result.status
        run.error = result.error
        run.total_tokens = result.total_tokens
        run.prompt_tokens = result.prompt_tokens
        run.completion_tokens = result.completion_tokens
        run.duration_ms = result.duration_ms
        run.step_count = result.step_count
        run.finished_at = datetime.utcnow()
        await session.commit()
        _publish_complete(run_id, run)
        steps = (
            await session.execute(
                select(Step).where(Step.run_id == run_id).order_by(Step.index.asc())
            )
        ).scalars().all()
        return _serialize_run(run, steps)


__all__ = ["router"]
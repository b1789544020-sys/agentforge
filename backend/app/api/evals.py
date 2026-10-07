"""Evaluation endpoints.

Two ways to trigger an evaluation:

* **Inline** ``cases`` — the client posts a list of ``EvalCaseIn`` items
  directly in the body.  The runner is invoked in the background with
  the legacy :meth:`EvalRunner.run` entry point.
* **Dataset refs** ``test_case_ids`` — the client references existing
  :class:`app.models.EvalCase` rows by id.  The runner uses
  :meth:`EvalRunner.run_eval` (composite scorer, per-case persisted
  results, ``/progress`` polling).

Both modes persist an :class:`app.models.EvalRun` row that the UI can
poll through the ``/{eval_id}`` and ``/{eval_id}/progress`` endpoints.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    EvalCaseOut,
    EvalCreate,
    EvalProgressOut,
    EvalRunOut,
    EvalUploadResponse,
)
from app.database import get_session
from app.eval.metrics import Metrics
from app.eval.runner import EvalRunner
from app.eval.scorer import EvalScorer
from app.models import Agent, EvalCase, EvalRun

router = APIRouter(prefix="/agents/{agent_id}/evals", tags=["evals"])


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------
def _serialize_case(case: EvalCase) -> dict[str, Any]:
    try:
        tool_calls = json.loads(case.tool_calls or "[]")
    except (TypeError, ValueError):
        tool_calls = []
    try:
        score_details = json.loads(case.score_details or "{}")
    except (TypeError, ValueError):
        score_details = {}
    return {
        "id": case.id,
        "index": case.index,
        "input": case.input,
        "expected": case.expected,
        "output": case.output,
        "score": case.score,
        "passed": bool(case.passed),
        "duration_ms": case.duration_ms,
        "tokens": case.tokens,
        "error": case.error,
        "tool_calls": tool_calls,
        "score_details": score_details,
    }


def _serialize_run(run: EvalRun, cases: list[EvalCase] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": run.id,
        "agent_id": run.agent_id,
        "dataset": run.dataset,
        # ``scorer`` is the field the UI reads on the run detail; fall
        # back to whatever the runner persisted (default: ``"composite"``).
        "scorer": run.scorer or "composite",
        "pass_threshold": float(run.pass_threshold or 0.0),
        "total": run.total,
        "passed": run.passed,
        "failed": run.failed,
        "average_score": run.average_score,
        "duration_ms": run.duration_ms,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "cases": [_serialize_case(c) for c in (cases or [])],
    }
    return payload


# Scorers that are implemented as classes in :mod:`app.eval.scorer` and
# resolved by the ``get_scorer`` factory.  Kept here so the validation
# step can produce a helpful error listing all available scorers.
_OBJECT_SCORERS = {"composite", "default", "status", "tool", "answer"}


def _resolve_scorer(name: str | None):
    """Resolve a scorer by name to an :class:`EvalScorer` instance.

    ``None`` returns ``None`` so the runner uses its own default.  Any
    other name is looked up via :func:`app.eval.scorer.get_scorer`
    (object scorers) or the legacy :class:`Metrics` registry.  Unknown
    names raise ``400`` with the list of available scorers.
    """
    if not name:
        return None  # runner falls back to its own default
    # Import lazily to avoid a circular import at module load time.
    from app.eval.scorer import get_scorer
    if name in _OBJECT_SCORERS:
        return get_scorer(name)
    registry = Metrics.registry()
    if name not in registry:
        raise HTTPException(
            status_code=400,
            detail=f"unknown scorer '{name}', available: {sorted(list(registry) + sorted(_OBJECT_SCORERS))}",
        )
    return registry[name]


# ---------------------------------------------------------------------------
# List + create
# ---------------------------------------------------------------------------
@router.get("", response_model=list[EvalRunOut])
async def list_eval_runs(
    agent_id: str,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(EvalRun)
            .where(EvalRun.agent_id == agent_id)
            .order_by(EvalRun.started_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [_serialize_run(r) for r in rows]


@router.post("", response_model=EvalRunOut, status_code=status.HTTP_201_CREATED)
async def create_eval(
    agent_id: str,
    payload: EvalCreate,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")

    # ---- input validation --------------------------------------------------
    has_inline = bool(payload.cases)
    has_dataset = bool(payload.test_case_ids)
    if not has_inline and not has_dataset:
        raise HTTPException(
            status_code=400,
            detail="either 'cases' or 'test_case_ids' must be provided",
        )

    scorer = _resolve_scorer(payload.scorer_name or payload.scorer)
    eval_run_id = str(uuid.uuid4())

    if has_dataset:
        # 404 when any referenced test case is missing.
        existing = (
            await session.execute(
                select(EvalCase.id).where(EvalCase.id.in_(payload.test_case_ids))
            )
        ).scalars().all()
        existing_set = set(existing)
        missing = [cid for cid in payload.test_case_ids if cid not in existing_set]
        if missing:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "test case(s) not found",
                    "missing": missing,
                },
            )
        total = len(payload.test_case_ids)
        cases_payload: list[dict[str, Any]] = []
    else:
        total = len(payload.cases)
        cases_payload = [c.model_dump() for c in payload.cases]

    # ---- create the placeholder EvalRun row -------------------------------
    placeholder = EvalRun(
        id=eval_run_id,
        agent_id=agent_id,
        dataset=payload.dataset_name,
        # Persist the scorer name on the row so the API/UI can read it
        # back without consulting the registry at request time.
        scorer=(payload.scorer_name or payload.scorer) or "composite",
        pass_threshold=float(payload.pass_threshold),
        total=total,
        passed=0,
        failed=0,
        average_score=0.0,
        status="pending",
    )
    session.add(placeholder)
    await session.commit()
    await session.refresh(placeholder)

    # ---- schedule the actual work in the background -----------------------
    async def _run() -> None:
        from app.database import async_session_factory

        async with async_session_factory() as inner:
            inner_agent = await inner.get(Agent, agent_id)
            if inner_agent is None:
                return
            # The constructor's ``scorer=`` slot is reserved for the
            # *legacy callable* form.  Object-style scorers are passed
            # directly to ``run_eval`` (the legacy ``run`` does not
            # accept a scorer override).
            legacy_scorer = None
            eval_scorer: Any = None
            if callable(scorer) and not isinstance(scorer, EvalScorer):
                legacy_scorer = scorer
            else:
                eval_scorer = scorer
            runner = EvalRunner(
                inner,
                inner_agent,
                scorer=legacy_scorer,
                pass_threshold=payload.pass_threshold,
                max_concurrency=payload.max_concurrency,
                # 0.1.2 5.1 轮: 并发 case 各用独立 session,
                # 共享 bookkeeping session 会因多 loop 抢连接死锁.
                session_factory=async_session_factory,
            )
            if has_dataset:
                await runner.run_eval(
                    payload.test_case_ids,
                    scorer=eval_scorer,
                    pass_threshold=payload.pass_threshold,
                    dataset=payload.dataset_name,
                    eval_run_id=eval_run_id,
                )
            else:
                await runner.run(
                    cases_payload,
                    dataset_name=payload.dataset_name,
                    eval_run_id=eval_run_id,
                )

    background.add_task(_run)
    return _serialize_run(placeholder, [])


# ---------------------------------------------------------------------------
# Detail + progress
# ---------------------------------------------------------------------------
@router.get("/{eval_id}", response_model=EvalRunOut)
async def get_eval(
    agent_id: str,
    eval_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = await session.get(EvalRun, eval_id)
    if not run or run.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="eval run not found")
    cases = (
        await session.execute(
            select(EvalCase)
            .where(EvalCase.eval_run_id == eval_id)
            .order_by(EvalCase.index.asc())
        )
    ).scalars().all()
    return _serialize_run(run, cases)


@router.get("/{eval_id}/progress", response_model=EvalProgressOut)
async def get_eval_progress(
    agent_id: str,
    eval_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return a poll-friendly snapshot of an in-flight eval run.

    Counts ``done`` as the number of :class:`EvalCase` rows already
    written for the run (one per executed case).  When the run is
    ``completed`` the computed ``progress`` is forced to ``1.0`` by
    :class:`EvalProgressOut.progress`.
    """
    run = await session.get(EvalRun, eval_id)
    if not run or run.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="eval run not found")
    done = (
        await session.execute(
            select(func.count(EvalCase.id)).where(EvalCase.eval_run_id == eval_id)
        )
    ).scalar_one()
    done = int(done or 0)
    return {
        "eval_run_id": run.id,
        "status": run.status,
        "total": int(run.total or 0),
        "passed": int(run.passed or 0),
        "failed": int(run.failed or 0),
        "done": done,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


@router.delete("/{eval_id}")
async def delete_eval(
    agent_id: str,
    eval_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    run = await session.get(EvalRun, eval_id)
    if not run or run.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="eval run not found")
    await session.delete(run)
    await session.commit()
    return {"ok": True, "deleted_id": eval_id}


# ---------------------------------------------------------------------------
# Multipart upload (0.1.3 #2)
# ---------------------------------------------------------------------------
def _safe_format(filename: str | None, text: str) -> str:
    """Best-effort format guess used in error responses."""
    from app.eval.parser import detect_format as _detect

    try:
        return _detect(filename, text)
    except Exception:  # noqa: BLE001
        return "unknown"


@router.post(
    "/upload",
    response_model=EvalUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_eval_dataset(
    agent_id: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    dataset_name: str = Form("default"),
    scorer_name: str | None = Form(None),
    pass_threshold: float = Form(0.7),
    max_concurrency: int = Form(4),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Upload a ``.jsonl`` or ``.csv`` dataset and run the agent over it."""
    from app.eval.parser import (
        DatasetParseError,
        detect_format as _detect,
        parse_dataset,
    )

    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")

    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"file is not valid utf-8: {e}",
        )

    if scorer_name:
        _resolve_scorer(scorer_name)  # raises HTTPException(400) on bad name

    try:
        fmt = _detect(file.filename, text)
        cases = parse_dataset(text, filename=file.filename, fmt=fmt)
    except DatasetParseError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "dataset parse failed",
                "format": _safe_format(file.filename, text),
                "errors": [err.to_dict() for err in e.errors],
            },
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": str(e),
                "format": _safe_format(file.filename, text),
            },
        )

    if not cases:
        raise HTTPException(
            status_code=400,
            detail={"error": "dataset contains no usable cases"},
        )

    # ---- create the EvalRun row + schedule the runner -----------------
    from app.database import async_session_factory

    eval_run_id = str(uuid.uuid4())
    placeholder = EvalRun(
        id=eval_run_id,
        agent_id=agent_id,
        dataset=dataset_name,
        scorer=scorer_name or "composite",
        pass_threshold=float(pass_threshold),
        total=len(cases),
        passed=0,
        failed=0,
        average_score=0.0,
        status="pending",
    )
    session.add(placeholder)
    await session.commit()
    await session.refresh(placeholder)

    payloads = [c.to_runner_payload() for c in cases]
    parsed_format = fmt

    async def _run() -> None:
        from app.eval.runner import EvalRunner as _Runner

        async with async_session_factory() as inner:
            inner_agent = await inner.get(Agent, agent_id)
            if inner_agent is None:
                return
            runner = _Runner(
                inner,
                inner_agent,
                scorer=None,
                pass_threshold=float(pass_threshold),
                max_concurrency=max_concurrency,
                session_factory=async_session_factory,
            )
            try:
                await runner.run(
                    payloads,
                    dataset_name=dataset_name,
                    eval_run_id=eval_run_id,
                )
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).exception(
                    "upload-eval run failed agent_id=%s eval_run_id=%s",
                    agent_id,
                    eval_run_id,
                )
                try:
                    inner_run = await inner.get(EvalRun, eval_run_id)
                    if inner_run is not None:
                        inner_run.status = "failed"
                        await inner.commit()
                except Exception:  # noqa: BLE001
                    pass

    background.add_task(_run)

    return EvalUploadResponse(
        run_id=eval_run_id,
        dataset_name=dataset_name,
        case_count=len(cases),
        accepted=len(cases),
        rejected=0,
        format=parsed_format,
    ).model_dump()



__all__ = ["router"]
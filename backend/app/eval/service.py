"""Durable evaluation execution shared by FastAPI and Celery workers."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select

from app.database import async_session_factory
from app.eval import control as eval_control
from app.eval.runner import EvalRunner
from app.eval.scorer import EvalScorer
from app.models import Agent, EvalCase, EvalRun, Run

logger = logging.getLogger(__name__)


def _request(row: EvalRun) -> dict[str, Any]:
    try:
        value = json.loads(row.request or "{}")
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        raise ValueError("eval request snapshot must be an object")
    return value


def _source_agent(agent: Agent, source: Run) -> Agent:
    try:
        snapshot = json.loads(source.config or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    return Agent(
        id=agent.id, name=agent.name, description=agent.description,
        system_prompt=agent.system_prompt,
        model=snapshot.get("model") or agent.model, provider=agent.provider,
        temperature=snapshot.get("temperature", agent.temperature),
        max_steps=snapshot.get("max_steps", agent.max_steps),
        tools=agent.tools, config=agent.config,
    )


async def execute_eval(eval_run_id: str) -> None:
    """Execute a persisted EvalRun; terminal retries are idempotent no-ops."""
    async with async_session_factory() as session:
        row = await session.get(EvalRun, eval_run_id)
        if row is None or row.status in {"completed", "failed", "cancelled"}:
            return
        agent = await session.get(Agent, row.agent_id)
        if agent is None:
            row.status = "failed"
            row.finished_at = datetime.utcnow()
            await session.commit()
            return
        request = _request(row)
        # A worker-lost retry is at-least-once. Remove partial result rows
        # before replay so progress and aggregates never double count.
        if row.status in {"running", "cancelling"}:
            await session.execute(delete(EvalCase).where(EvalCase.eval_run_id == eval_run_id))
            await session.commit()
        from app.api.evals import _resolve_scorer

        scorer = _resolve_scorer(row.scorer or "composite")
        event = eval_control.register(eval_run_id)
        runner = EvalRunner(
            session, agent,
            scorer=None if isinstance(scorer, EvalScorer) else scorer,
            pass_threshold=row.pass_threshold,
            max_concurrency=int(request.get("max_concurrency", 2)),
            session_factory=async_session_factory,
            cancel_event=event,
            priority=row.priority,
        )
        try:
            kind = request.get("kind")
            if kind == "source_run":
                source = await session.get(Run, request["source_run_id"])
                if source is None:
                    raise ValueError("source run no longer exists")
                runner.agent = _source_agent(agent, source)
                if isinstance(scorer, EvalScorer):
                    await runner.run_eval(
                        [request["case_id"]], scorer=scorer,
                        pass_threshold=row.pass_threshold,
                        dataset=row.dataset, eval_run_id=eval_run_id,
                    )
                else:
                    await runner.run(
                        [{"input": source.input, "expected": request.get("expected")}],
                        dataset_name=row.dataset, eval_run_id=eval_run_id,
                    )
                return
            if kind == "dataset" and isinstance(scorer, EvalScorer):
                await runner.run_eval(
                    request.get("test_case_ids", []), scorer=scorer,
                    pass_threshold=row.pass_threshold,
                    dataset=row.dataset, eval_run_id=eval_run_id,
                )
                return
            cases = list(request.get("cases", []))
            if kind == "dataset":
                ids = list(request.get("test_case_ids", []))
                rows = (
                    await session.execute(select(EvalCase).where(EvalCase.id.in_(ids)))
                ).scalars().all() if ids else []
                by_id = {case.id: case for case in rows}
                cases = [
                    {"input": by_id[case_id].input, "expected": by_id[case_id].expected}
                    for case_id in ids if case_id in by_id
                ]
            if isinstance(scorer, EvalScorer):
                case_ids = request.get("case_ids")
                if not case_ids:
                    case_ids = []
                    for index, raw in enumerate(cases):
                        case_id = str(uuid.uuid4())
                        case_ids.append(case_id)
                        session.add(EvalCase(
                            id=case_id, agent_id=agent.id, owner_id=agent.owner_id, team_id=agent.team_id,
                            dataset=row.dataset, name=f"case-{index}",
                            input=str(raw.get("input", "")),
                            expected=None if raw.get("expected") is None else str(raw.get("expected")),
                            meta=json.dumps(raw.get("metadata") or {}, ensure_ascii=False), index=index,
                        ))
                    request["case_ids"] = case_ids
                    row.request = json.dumps(request, ensure_ascii=False)
                    await session.commit()
                await runner.run_eval(
                    case_ids, scorer=scorer, pass_threshold=row.pass_threshold,
                    dataset=row.dataset, eval_run_id=eval_run_id,
                )
            else:
                await runner.run(cases, dataset_name=row.dataset, eval_run_id=eval_run_id)
        except Exception:
            logger.exception("Persistent eval failed eval_run_id=%s", eval_run_id)
            row.status = "failed"
            row.finished_at = datetime.utcnow()
            await session.commit()
            raise
        finally:
            await eval_control.unregister_async(eval_run_id)


__all__ = ["execute_eval"]

"""Quality-gate execution, regression diffing, and trigger scheduling."""
from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.versions import agent_from_version
from app.audit import record_audit
from app.database import async_session_factory
from app.eval.runner import EvalRunner
from app.eval.scorer import EvalScorer
from app.models import Agent, AgentVersion, EvalCase, EvalRun, EvalSuite, GatePolicy, GateRun
from app.quality.conditions import build_gate_conditions
from app.quality.evidence import build_gate_evidence, enrich_case_diffs, validate_gate_record
from app.rbac import eval_case_scope

logger = logging.getLogger(__name__)


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except (TypeError, ValueError):
        return fallback


def build_case_diff(current: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify per-case changes and include a readable answer diff."""
    old = {str(item["case_id"]): item for item in previous}
    new = {str(item["case_id"]): item for item in current}
    output: list[dict[str, Any]] = []
    for case_id in list(new) + [case_id for case_id in old if case_id not in new]:
        before = old.get(case_id)
        after = new.get(case_id)
        if before is None:
            change = "new_case"
        elif after is None:
            change = "removed"
        elif before["passed"] and not after["passed"]:
            change = "new_failure"
        elif not before["passed"] and after["passed"]:
            change = "fixed"
        else:
            change = "unchanged"
        answer_diff = ""
        if before is not None and after is not None:
            answer_diff = "\n".join(difflib.unified_diff(
                str(before.get("output") or "").splitlines(),
                str(after.get("output") or "").splitlines(),
                fromfile="previous", tofile="current", lineterm="",
            ))
        output.append({
            "case_id": case_id, "change": change,
            "previous_passed": before.get("passed") if before else None,
            "current_passed": after.get("passed") if after else None,
            "answer_diff": answer_diff,
        })
    return output


async def _refresh_version_status(session: AsyncSession, version_id: str) -> None:
    version = await session.get(AgentVersion, version_id)
    if version is None:
        return
    statuses = list((await session.execute(
        select(GateRun.status).where(GateRun.agent_version_id == version_id)
    )).scalars().all())
    if any(status in {"failed", "error"} for status in statuses):
        version.gate_status = "failed"
    elif not statuses or any(status in {"pending", "running"} for status in statuses):
        version.gate_status = "pending"
    else:
        version.gate_status = "passed"


async def create_gate_run(
    session: AsyncSession, *, agent_version_id: str, suite_id: str,
    triggered_by: str | None, trigger_source: str = "manual",
) -> GateRun:
    version = await session.get(AgentVersion, agent_version_id)
    suite = await session.get(EvalSuite, suite_id)
    if version is None or suite is None:
        raise ValueError("agent version or suite not found")
    if suite.agent_id is not None and suite.agent_id != version.agent_id:
        raise ValueError("suite belongs to a different agent")
    if not suite.enabled:
        raise ValueError("suite is disabled")
    case_ids = [str(value) for value in _json(suite.case_ids, [])]
    if not case_ids:
        raise ValueError("suite has no cases")
    found = set((await session.execute(
        select(EvalCase.id).where(
            EvalCase.id.in_(case_ids),
            eval_case_scope(Agent(id=version.agent_id, team_id=version.team_id, owner_id=suite.owner_id)),
        )
    )).scalars().all())
    missing = [case_id for case_id in case_ids if case_id not in found]
    if missing:
        raise ValueError(f"suite cases not found: {', '.join(missing)}")
    policy = await session.get(GatePolicy, "default")
    threshold = suite.pass_threshold if suite.pass_threshold is not None else (
        policy.default_threshold if policy else 0.95
    )
    baseline = (await session.execute(
        select(GateRun).join(AgentVersion, GateRun.agent_version_id == AgentVersion.id)
        .where(GateRun.suite_id == suite.id, AgentVersion.agent_id == version.agent_id,
               AgentVersion.version_number < version.version_number,
               GateRun.status.in_(["passed", "failed"]))
        .order_by(AgentVersion.version_number.desc(), GateRun.created_at.desc()).limit(1)
    )).scalars().first()
    conditions = await build_gate_conditions(session, version, suite, float(threshold))
    eval_run = EvalRun(
        agent_id=version.agent_id, team_id=version.team_id, dataset=f"gate:{suite.name}", scorer=suite.scorer,
        pass_threshold=0.7, request=json.dumps({
            "kind": "gate", "agent_version_id": version.id, "suite_id": suite.id,
            "max_concurrency": 2,
        }), total=len(case_ids), status="pending",
    )
    session.add(eval_run)
    await session.flush()
    gate = GateRun(
        agent_version_id=version.id, suite_id=suite.id, eval_run_id=eval_run.id,
        baseline_gate_id=baseline.id if baseline else None, triggered_by=triggered_by,
        trigger_source=trigger_source, status="pending", threshold=float(threshold),
        quality_record_version=1, conditions=json.dumps(conditions, ensure_ascii=False),
    )
    session.add(gate)
    await session.flush()
    record_audit(
        session, None, action="gate.trigger", target_type="gate_run", target_id=gate.id,
        team_id=version.team_id, actor_id=triggered_by,
        detail={"agent_version_id": version.id, "suite_id": suite.id, "trigger_source": trigger_source},
    )
    version.gate_status = "pending"
    await session.commit()
    await session.refresh(gate)
    return gate


async def execute_gate(gate_run_id: str) -> None:
    """Execute a gate from its persisted request; terminal retries are no-ops."""
    async with async_session_factory() as session:
        gate = await session.get(GateRun, gate_run_id)
        if gate is None or gate.status in {"passed", "failed"}:
            return
        version = await session.get(AgentVersion, gate.agent_version_id)
        suite = await session.get(EvalSuite, gate.suite_id)
        eval_run = await session.get(EvalRun, gate.eval_run_id)
        if version is None or suite is None or eval_run is None:
            raise ValueError("gate execution dependencies no longer exist")
        gate.status = "running"
        eval_run.status = "running"
        await session.commit()
        try:
            from app.api.evals import _resolve_scorer
            scorer = _resolve_scorer(suite.scorer)
            if not isinstance(scorer, EvalScorer):
                raise ValueError("quality gates require a structured EvalScorer")
            agent = agent_from_version(version)
            runner = EvalRunner(
                session, agent, pass_threshold=0.7, max_concurrency=2,
                session_factory=async_session_factory,
            )
            result = await runner.run_eval(
                _json(suite.case_ids, []), scorer=scorer, pass_threshold=0.7,
                dataset=eval_run.dataset, eval_run_id=eval_run.id,
            )
            # EvalRunner expunges its bookkeeping session; reload exact records.
            gate = await session.get(GateRun, gate_run_id)
            suite = await session.get(EvalSuite, suite.id)
            version = await session.get(AgentVersion, version.id)
            if gate is None or suite is None or version is None:
                raise ValueError("gate disappeared during execution")
            conditions = _json(gate.conditions, {})
            evidence = await build_gate_evidence(session, gate, result.cases, conditions)
            by_case = {item["case_id"]: item for item in evidence}
            details = [{
                "case_id": case.test_case_id, "result_case_id": case.case_id,
                "input": case.input, "expected": case.expected_answer,
                "output": case.output, "score": case.score,
                "passed": by_case[case.test_case_id]["passed"],
                "error": case.error, "duration_ms": case.duration_ms, "tokens": case.tokens,
                "run_ids": [case.run_id], "attempt_count": 1,
                "failed_layers": by_case[case.test_case_id]["failed_layers"],
            } for case in result.cases]
            baseline = await session.get(GateRun, gate.baseline_gate_id) if gate.baseline_gate_id else None
            previous = _json(baseline.details, []) if baseline else []
            previous_evidence = _json(baseline.evidence, []) if baseline else []
            pass_rate = sum(1 for item in details if item["passed"]) / len(details) if details else 0.0
            gate.pass_rate = pass_rate
            gate.threshold = float(suite.pass_threshold)
            gate.failed_case_ids = json.dumps(
                [item["case_id"] for item in details if not item["passed"]], ensure_ascii=False
            )
            gate.details = json.dumps(details, ensure_ascii=False)
            basic_diff = build_case_diff(details, previous)
            diffs = enrich_case_diffs(
                basic_diff, details, previous, gate, baseline, evidence, previous_evidence,
            )
            gate.diff = json.dumps(diffs, ensure_ascii=False)
            gate.evidence = json.dumps(evidence, ensure_ascii=False)
            gate.status = "passed" if pass_rate >= gate.threshold else "failed"
            gate.finished_at = datetime.utcnow()
            gate.error = None
            await validate_gate_record(session, gate, details, evidence, diffs)
            await session.flush()
            await _refresh_version_status(session, version.id)
            await session.commit()
        except Exception as exc:
            logger.exception("Quality gate failed gate_run_id=%s", gate_run_id)
            await session.rollback()
            gate = await session.get(GateRun, gate_run_id)
            if gate is not None:
                gate.status = "failed"
                gate.error = f"{exc.__class__.__name__}: {exc}"
                gate.finished_at = datetime.utcnow()
                eval_run = await session.get(EvalRun, gate.eval_run_id)
                if eval_run is not None:
                    eval_run.status = "failed"
                    eval_run.finished_at = datetime.utcnow()
                await _refresh_version_status(session, gate.agent_version_id)
                await session.commit()
            raise


async def reevaluate_gate(session: AsyncSession, gate_run_id: str) -> GateRun:
    """Reapply the suite's current pass-rate threshold without rewriting cases."""
    gate = await session.get(GateRun, gate_run_id)
    if gate is None or not gate.details:
        raise ValueError("completed gate not found")
    suite = await session.get(EvalSuite, gate.suite_id)
    if suite is None:
        raise ValueError("suite not found")
    gate.threshold = float(suite.pass_threshold)
    gate.status = "passed" if gate.pass_rate >= gate.threshold else "failed"
    await _refresh_version_status(session, gate.agent_version_id)
    await session.commit()
    await session.refresh(gate)
    return gate



async def create_automatic_gates(
    session: AsyncSession, version: AgentVersion, *, triggered_by: str | None
) -> list[GateRun]:
    suites = (await session.execute(
        select(EvalSuite).where(
            EvalSuite.enabled.is_(True), EvalSuite.auto_on_save.is_(True),
            (EvalSuite.agent_id == version.agent_id) | EvalSuite.agent_id.is_(None),
        ).order_by(EvalSuite.created_at)
    )).scalars().all()
    gates: list[GateRun] = []
    for suite in suites:
        gates.append(await create_gate_run(
            session, agent_version_id=version.id, suite_id=suite.id,
            triggered_by=triggered_by, trigger_source="save",
        ))
    return gates


async def execute_due_gates(now: datetime | None = None) -> list[str]:
    """Run due per-agent schedules; returns executed GateRun ids."""
    current = now or datetime.utcnow()
    async with async_session_factory() as session:
        suites = (await session.execute(
            select(EvalSuite).where(
                EvalSuite.enabled.is_(True), EvalSuite.agent_id.is_not(None),
                EvalSuite.schedule_interval_seconds.is_not(None),
                EvalSuite.next_run_at.is_not(None), EvalSuite.next_run_at <= current,
            )
        )).scalars().all()
        due: list[str] = []
        for suite in suites:
            version = (await session.execute(
                select(AgentVersion).where(AgentVersion.agent_id == suite.agent_id)
                .order_by(AgentVersion.version_number.desc()).limit(1)
            )).scalars().first()
            interval = int(suite.schedule_interval_seconds or 0)
            suite.next_run_at = current + timedelta(seconds=interval)
            await session.flush()
            if version is not None:
                gate = await create_gate_run(
                    session, agent_version_id=version.id, suite_id=suite.id,
                    triggered_by=None, trigger_source="scheduled",
                )
                due.append(gate.id)
            await session.commit()
    for gate_id in due:
        await execute_gate(gate_id)
    return due


__all__ = [
    "build_case_diff",
    "create_automatic_gates",
    "create_gate_run",
    "execute_due_gates",
    "execute_gate",
    "reevaluate_gate",
]

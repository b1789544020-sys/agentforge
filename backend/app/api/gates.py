"""Agent version, evaluation suite, and quality-gate HTTP endpoints."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.agent.schema import public_definition, public_legacy_dto
from app.api.auth import get_principal, require_admin
from app.api.schemas import (
    EvalSuiteCreate,
    EvalSuiteUpdate,
    GateBypassCreate,
    GatePolicyUpdate,
    GateTrigger,
)
from app.audit import record_audit
from app.database import get_session
from app.dispatch import submit_gate
from app.gates.service import create_gate_run, execute_due_gates, reevaluate_gate
from app.models import Agent, AgentVersion, EvalCase, EvalSuite, GateBypass, GatePolicy, GateRun
from app.rbac import accessible_team_ids, authorize, eval_case_scope, scoped_agent_ids, team_role

router = APIRouter(tags=["quality-gates"])


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except (TypeError, ValueError):
        return fallback


def _actor(request: Request) -> str | None:
    return get_principal(request).user_id


def version_out(row: AgentVersion) -> dict[str, Any]:
    return public_legacy_dto({
        "id": row.id, "agent_id": row.agent_id, "team_id": row.team_id,
        "version_number": row.version_number,
        "snapshot": _json(row.snapshot, {}), "system_prompt": row.system_prompt,
        "model": row.model, "provider": row.provider, "temperature": row.temperature,
        "max_steps": row.max_steps, "tools": _json(row.tools, []), "config": _json(row.config, {}),
        "gate_status": row.gate_status, "created_by": row.created_by, "created_at": row.created_at,
    })


def suite_out(row: EvalSuite) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "description": row.description,
        "owner_id": row.owner_id, "team_id": row.team_id, "agent_id": row.agent_id,
        "case_ids": _json(row.case_ids, []), "pass_threshold": row.pass_threshold,
        "scorer": row.scorer, "enabled": row.enabled, "auto_on_save": row.auto_on_save,
        "schedule_interval_seconds": row.schedule_interval_seconds, "next_run_at": row.next_run_at,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def gate_out(row: GateRun, *, include_quality: bool = False) -> dict[str, Any]:
    output = {
        "id": row.id, "agent_version_id": row.agent_version_id, "suite_id": row.suite_id,
        "eval_run_id": row.eval_run_id, "baseline_gate_id": row.baseline_gate_id,
        "triggered_by": row.triggered_by, "trigger_source": row.trigger_source,
        "status": row.status, "pass_rate": row.pass_rate, "threshold": row.threshold,
        "failed_case_ids": _json(row.failed_case_ids, []), "details": _json(row.details, []),
        "diff": _json(row.diff, []), "error": row.error, "created_at": row.created_at,
        "finished_at": row.finished_at,
    }
    if include_quality:
        output.update({
            "quality_record_version": row.quality_record_version,
            "conditions": _json(row.conditions, None),
            "evidence": _json(row.evidence, None),
        })
    return output


async def _validate_cases(session: AsyncSession, case_ids: list[str], agent: Agent | None = None) -> None:
    if len(set(case_ids)) != len(case_ids):
        raise HTTPException(400, "suite case ids must be unique")
    stmt = select(EvalCase.id).where(EvalCase.id.in_(case_ids))
    if agent is not None:
        stmt = stmt.where(eval_case_scope(agent))
    found = set((await session.execute(stmt)).scalars().all())
    missing = [case_id for case_id in case_ids if case_id not in found]
    if missing:
        raise HTTPException(404, {"error": "test cases not found", "missing": missing})


async def _suite_access(
    request: Request, session: AsyncSession, suite: EvalSuite | None, *, write: bool = False,
) -> EvalSuite:
    if suite is None:
        raise HTTPException(404, "suite not found")
    principal = get_principal(request)
    if principal.role != "admin":
        visible = False
        if suite.team_id:
            visible = await team_role(session, principal.user_id or "", suite.team_id) is not None
        elif suite.agent_id:
            visible = suite.agent_id in (await scoped_agent_ids(session, request) or set())
        else:
            visible = suite.owner_id == principal.user_id
        if not visible:
            raise HTTPException(404, "suite not found")
        await authorize(request, session, "gate:trigger" if write else "gate:read", suite.team_id)
    return suite


async def _policy(session: AsyncSession) -> GatePolicy:
    row = await session.get(GatePolicy, "default")
    if row is None:
        row = GatePolicy(id="default")
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


@router.get("/agents/{agent_id}/versions")
async def list_versions(agent_id: str, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = (await session.execute(
        select(AgentVersion).where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version_number.desc())
    )).scalars().all()
    return [version_out(row) for row in rows]


@router.get("/agents/{agent_id}/versions/{version_id}/definition")
async def get_version_definition(
    agent_id: str, version_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    row = await session.get(AgentVersion, version_id)
    if row is None or row.agent_id != agent_id:
        raise HTTPException(404, "agent version not found")
    snapshot = _json(row.snapshot, {})
    if not isinstance(snapshot, dict):
        raise HTTPException(409, "agent version snapshot is invalid")
    return public_definition(snapshot)


@router.get("/agents/{agent_id}/versions/{version_id}")
async def get_version(agent_id: str, version_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await session.get(AgentVersion, version_id)
    if row is None or row.agent_id != agent_id:
        raise HTTPException(404, "agent version not found")
    return version_out(row)


@router.get("/suites")
async def list_suites(request: Request, agent_id: str | None = None, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(EvalSuite).order_by(EvalSuite.created_at.desc())
    if agent_id:
        stmt = stmt.where((EvalSuite.agent_id == agent_id) | EvalSuite.agent_id.is_(None))
    teams = await accessible_team_ids(session, request)
    if teams is not None:
        agent_ids = await scoped_agent_ids(session, request) or set()
        conditions = []
        if teams:
            conditions.append(EvalSuite.team_id.in_(teams))
        if agent_ids:
            conditions.append(EvalSuite.agent_id.in_(agent_ids))
        conditions.append(and_(EvalSuite.team_id.is_(None), EvalSuite.owner_id == get_principal(request).user_id))
        stmt = stmt.where(or_(*conditions))
    return [suite_out(row) for row in (await session.execute(stmt)).scalars().all()]


@router.post("/suites", status_code=201)
async def create_suite(payload: EvalSuiteCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    agent: Agent | None = None
    if payload.agent_id:
        agent = await session.get(Agent, payload.agent_id)
        if agent is None:
            raise HTTPException(404, "agent not found")
        if get_principal(request).role != "admin":
            if agent.id not in (await scoped_agent_ids(session, request) or set()):
                raise HTTPException(404, "agent not found")
            await authorize(request, session, "gate:trigger", agent.team_id)
    elif get_principal(request).role != "admin":
        raise HTTPException(403, "non-admin suites must belong to an agent")
    await _validate_cases(session, payload.case_ids, agent)
    threshold = payload.pass_threshold
    if threshold is None:
        threshold = (await _policy(session)).default_threshold
    next_run = (datetime.utcnow() + timedelta(seconds=payload.schedule_interval_seconds)
                if payload.schedule_interval_seconds else None)
    row = EvalSuite(
        name=payload.name, description=payload.description, agent_id=payload.agent_id,
        owner_id=_actor(request), team_id=agent.team_id if agent else None,
        case_ids=json.dumps(payload.case_ids), pass_threshold=threshold,
        scorer=payload.scorer, enabled=payload.enabled, auto_on_save=payload.auto_on_save,
        schedule_interval_seconds=payload.schedule_interval_seconds, next_run_at=next_run,
    )
    session.add(row)
    await session.flush()
    record_audit(session, request, action="suite.create", target_type="eval_suite", target_id=row.id,
                 team_id=row.team_id, detail={"agent_id": row.agent_id, "name": row.name})
    await session.commit()
    await session.refresh(row)
    return suite_out(row)


@router.get("/suites/{suite_id}")
async def get_suite(suite_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _suite_access(request, session, await session.get(EvalSuite, suite_id))
    return suite_out(row)


@router.patch("/suites/{suite_id}")
async def update_suite(suite_id: str, payload: EvalSuiteUpdate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _suite_access(request, session, await session.get(EvalSuite, suite_id), write=True)
    data = payload.model_dump(exclude_unset=True)
    if "case_ids" in data:
        agent = await session.get(Agent, row.agent_id) if row.agent_id else None
        await _validate_cases(session, data["case_ids"], agent)
        data["case_ids"] = json.dumps(data.pop("case_ids"))
    if "schedule_interval_seconds" in data:
        interval = data["schedule_interval_seconds"]
        row.next_run_at = datetime.utcnow() + timedelta(seconds=interval) if interval else None
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(row)
    return suite_out(row)


@router.post("/suites/{suite_id}/copy", status_code=201)
async def copy_suite(suite_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    source = await _suite_access(request, session, await session.get(EvalSuite, suite_id), write=True)
    row = EvalSuite(
        name=f"{source.name} (copy)", description=source.description, agent_id=source.agent_id,
        owner_id=_actor(request), team_id=source.team_id, case_ids=source.case_ids,
        pass_threshold=source.pass_threshold, scorer=source.scorer, enabled=False,
        auto_on_save=source.auto_on_save, schedule_interval_seconds=source.schedule_interval_seconds,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return suite_out(row)


@router.delete("/suites/{suite_id}", status_code=204, response_model=None)
async def delete_suite(suite_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> None:
    row = await _suite_access(request, session, await session.get(EvalSuite, suite_id), write=True)
    record_audit(session, request, action="suite.delete", target_type="eval_suite", target_id=row.id,
                 team_id=row.team_id, detail={"name": row.name})
    await session.delete(row)
    await session.commit()


@router.post("/agents/{agent_id}/gates", status_code=202)
async def trigger_gate(
    agent_id: str, payload: GateTrigger, background: BackgroundTasks,
    request: Request, session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    version = await session.get(AgentVersion, payload.agent_version_id)
    if version is None or version.agent_id != agent_id:
        raise HTTPException(404, "agent version not found")
    try:
        row = await create_gate_run(
            session, agent_version_id=payload.agent_version_id, suite_id=payload.suite_id,
            triggered_by=_actor(request), trigger_source=payload.trigger_source,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    submit_gate(row.id, background)
    return gate_out(row)


@router.get("/agents/{agent_id}/gates")
async def list_gates(
    agent_id: str, suite_id: str | None = None, session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    stmt = select(GateRun).join(AgentVersion, GateRun.agent_version_id == AgentVersion.id).options(
        defer(GateRun.conditions), defer(GateRun.evidence),
    ).where(AgentVersion.agent_id == agent_id)
    if suite_id:
        stmt = stmt.where(GateRun.suite_id == suite_id)
    rows = (await session.execute(stmt.order_by(GateRun.created_at.desc()).limit(100))).scalars().all()
    return [gate_out(row) for row in rows]


async def _nested_gate(session: AsyncSession, agent_id: str, gate_id: str) -> GateRun:
    row = await session.get(GateRun, gate_id)
    version = await session.get(AgentVersion, row.agent_version_id) if row else None
    if row is None or version is None or version.agent_id != agent_id:
        raise HTTPException(404, "gate run not found")
    return row


@router.get("/agents/{agent_id}/gates/{gate_id}")
async def get_gate(agent_id: str, gate_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return gate_out(await _nested_gate(session, agent_id, gate_id), include_quality=True)


@router.post("/agents/{agent_id}/gates/{gate_id}/reevaluate")
async def reevaluate(agent_id: str, gate_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _nested_gate(session, agent_id, gate_id)
    return gate_out(await reevaluate_gate(session, row.id))


@router.get("/gate-policy")
async def get_gate_policy(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _policy(session)
    return {"id": row.id, "default_threshold": row.default_threshold, "enforce": row.enforce,
            "allow_bypass": row.allow_bypass, "notify_to": _json(row.notify_to, []), "updated_at": row.updated_at}


@router.patch("/gate-policy", dependencies=[Depends(require_admin)])
async def update_gate_policy(payload: GatePolicyUpdate, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _policy(session)
    data = payload.model_dump(exclude_unset=True)
    if "notify_to" in data:
        data["notify_to"] = json.dumps(data["notify_to"])
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_at = datetime.utcnow()
    await session.commit()
    return {"id": row.id, "default_threshold": row.default_threshold, "enforce": row.enforce,
            "allow_bypass": row.allow_bypass, "notify_to": _json(row.notify_to, []), "updated_at": row.updated_at}


@router.post("/agents/{agent_id}/versions/{version_id}/bypass", status_code=201)
async def bypass_gate(
    agent_id: str, version_id: str, payload: GateBypassCreate, request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    version = await session.get(AgentVersion, version_id)
    if version is None or version.agent_id != agent_id:
        raise HTTPException(404, "agent version not found")
    policy = await _policy(session)
    if not policy.allow_bypass:
        raise HTTPException(403, "gate bypass is disabled by policy")
    if payload.gate_run_id:
        gate = await _nested_gate(session, agent_id, payload.gate_run_id)
        if gate.agent_version_id != version_id:
            raise HTTPException(400, "gate does not belong to this version")
    row = GateBypass(
        agent_version_id=version_id, gate_run_id=payload.gate_run_id,
        created_by=_actor(request), reason=payload.reason.strip(),
    )
    version.gate_status = "bypassed"
    session.add(row)
    await session.flush()
    record_audit(session, request, action="gate.override", target_type="gate_bypass", target_id=row.id,
                 team_id=version.team_id, detail={"agent_version_id": version_id,
                 "gate_run_id": payload.gate_run_id, "reason": payload.reason})
    await session.commit()
    await session.refresh(row)
    return {"id": row.id, "agent_version_id": row.agent_version_id, "gate_run_id": row.gate_run_id,
            "created_by": row.created_by, "reason": row.reason, "active": row.active, "created_at": row.created_at}


@router.post("/gates/run-due", dependencies=[Depends(require_admin)])
async def run_due_gates() -> dict[str, Any]:
    return {"executed": await execute_due_gates()}


__all__ = ["gate_out", "router", "suite_out", "version_out"]

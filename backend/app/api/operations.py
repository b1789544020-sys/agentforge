"""Operations dashboard, cost, feedback, and in-app alert APIs."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_principal, require_admin
from app.audit import record_audit
from app.database import get_session
from app.models import (
    Agent,
    AgentVersion,
    AlertEvent,
    AlertRule,
    EvalCase,
    EvalSuite,
    Feedback,
    FeedbackResolution,
    GateRun,
    ModelPrice,
    Run,
)
from app.operations import aggregate_metrics, calculate_costs, evaluate_alert_rules
from app.quality.feedback import (
    FeedbackRecord,
    validate_feedback_resolution,
)
from app.quality.feedback import (
    FeedbackResolution as FeedbackResolutionContract,
)
from app.quality.gate import GateRecordV1
from app.rbac import accessible_team_ids, authorize, scoped_agent_ids
from app.secrets import redact_config

router = APIRouter(prefix="/operations", tags=["operations"])


class PriceCreate(BaseModel):
    provider: str
    model: str
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    currency: str = Field(default="USD", max_length=8)
    effective_from: datetime


class FeedbackCreate(BaseModel):
    run_id: str
    rating: str
    expected_answer: str | None = None

    @field_validator("rating")
    @classmethod
    def valid_rating(cls, value: str) -> str:
        if value not in {"positive", "negative"}:
            raise ValueError("rating must be positive or negative")
        return value


class FeedbackConvert(BaseModel):
    expected_answer: str = Field(min_length=1)
    suite_id: str | None = None


class FeedbackResolutionCreate(BaseModel):
    gate_run_id: str
    case_id: str
    verdict: Literal["needs_review", "confirmed_fixed", "not_fixed", "conditions_changed"]
    note: str = Field(min_length=1, max_length=4000)


class AlertRuleCreate(BaseModel):
    team_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    kind: str
    threshold: float = Field(gt=0)
    enabled: bool = True

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in {"failure_rate", "daily_cost", "error_spike", "gate_failures"}:
            raise ValueError("unsupported alert rule kind")
        return value


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except (TypeError, ValueError):
        return fallback


async def _run_scope(request: Request, session: AsyncSession, team_id: str | None = None):
    teams = await accessible_team_ids(session, request)
    if teams is None:
        return Run.team_id == team_id if team_id else None
    if team_id:
        if team_id not in teams:
            raise HTTPException(404, "team not found")
        return Run.team_id == team_id
    agent_ids = await scoped_agent_ids(session, request) or set()
    conditions = []
    if teams:
        conditions.append(Run.team_id.in_(teams))
    if agent_ids:
        conditions.append(Run.agent_id.in_(agent_ids))
    return or_(*conditions) if conditions else Run.id.is_(None)


async def _filtered_runs(
    request: Request, session: AsyncSession, *, agent_id: str | None, team_id: str | None,
    model: str | None, since: datetime | None, until: datetime | None,
) -> tuple[list[Run], dict[str, Agent]]:
    scope = await _run_scope(request, session, team_id)
    stmt = select(Run)
    if scope is not None:
        stmt = stmt.where(scope)
    if agent_id:
        stmt = stmt.where(Run.agent_id == agent_id)
    if since:
        stmt = stmt.where(Run.started_at >= since)
    if until:
        stmt = stmt.where(Run.started_at <= until)
    runs = list((await session.execute(stmt.order_by(Run.started_at))).scalars().all())
    agents = {row.id: row for row in (await session.execute(select(Agent))).scalars().all()}
    if model:
        runs = [run for run in runs if model in {
            str(_json(run.config, {}).get("model") or ""), getattr(agents.get(run.agent_id), "model", "")}
        ]
    return runs, agents


def _price_out(row: ModelPrice) -> dict[str, Any]:
    return {"id": row.id, "provider": row.provider, "model": row.model,
            "input_per_million": row.input_per_million, "output_per_million": row.output_per_million,
            "currency": row.currency, "effective_from": row.effective_from, "created_at": row.created_at}


@router.get("/metrics")
async def metrics(
    request: Request, agent_id: str | None = None, team_id: str | None = None,
    model: str | None = None, since: datetime | None = None, until: datetime | None = None,
    group: str = "day", session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        runs, agents = await _filtered_runs(request, session, agent_id=agent_id, team_id=team_id,
                                           model=model, since=since, until=until)
        return aggregate_metrics(runs, agents, group)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/cost")
async def cost(
    request: Request, agent_id: str | None = None, team_id: str | None = None,
    model: str | None = None, since: datetime | None = None, until: datetime | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    runs, agents = await _filtered_runs(request, session, agent_id=agent_id, team_id=team_id,
                                       model=model, since=since, until=until)
    prices = (await session.execute(select(ModelPrice))).scalars().all()
    return calculate_costs(runs, agents, list(prices))


@router.get("/prices")
async def list_prices(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = (await session.execute(select(ModelPrice).order_by(ModelPrice.effective_from.desc()))).scalars().all()
    return [_price_out(row) for row in rows]


@router.post("/prices", status_code=201, dependencies=[Depends(require_admin)])
async def create_price(payload: PriceCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = ModelPrice(**payload.model_dump(), created_by=get_principal(request).user_id)
    session.add(row)
    await session.commit()
    return _price_out(row)


def _feedback_out(row: Feedback) -> dict[str, Any]:
    return {"id": row.id, "run_id": row.run_id, "agent_id": row.agent_id, "team_id": row.team_id,
            "agent_version_id": row.agent_version_id, "rating": row.rating, "expected_answer": row.expected_answer,
            "context_snapshot": redact_config(_json(row.context_snapshot, {})),
            "eval_case_id": row.eval_case_id, "suite_id": row.suite_id,
            "created_by": row.created_by, "created_at": row.created_at}


@router.post("/feedback", status_code=201)
async def mark_feedback(payload: FeedbackCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    scope = await _run_scope(request, session)
    stmt = select(Run).where(Run.id == payload.run_id)
    if scope is not None:
        stmt = stmt.where(scope)
    run = (await session.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "run not found")
    version = await session.get(AgentVersion, run.agent_version_id) if run.agent_version_id else None
    if version is None:
        version = (await session.execute(select(AgentVersion).where(
            AgentVersion.agent_id == run.agent_id, AgentVersion.created_at <= run.started_at
        ).order_by(AgentVersion.version_number.desc()).limit(1))).scalars().first()
    context = {
        "input": run.input, "output": run.output, "run_id": run.id,
        "agent_version_id": version.id if version else run.agent_version_id,
        "agent_snapshot": _json(version.snapshot, {}) if version else {},
    }
    row = (await session.execute(select(Feedback).where(Feedback.run_id == run.id))).scalar_one_or_none()
    if row is None:
        row = Feedback(run_id=run.id, agent_id=run.agent_id, team_id=run.team_id,
                       agent_version_id=run.agent_version_id,
                       created_by=get_principal(request).user_id, context_snapshot=json.dumps(context, ensure_ascii=False))
        session.add(row)
    elif row.agent_version_id is None:
        row.agent_version_id = run.agent_version_id
    row.rating = payload.rating
    row.expected_answer = payload.expected_answer
    await session.flush()
    record_audit(session, request, action="feedback.mark", target_type="feedback", target_id=row.id,
                 team_id=row.team_id, detail={"run_id": run.id, "rating": row.rating})
    await session.commit()
    return _feedback_out(row)


@router.get("/feedback")
async def list_feedback(request: Request, rating: str | None = None, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    scope = await _run_scope(request, session)
    stmt = select(Feedback).join(Run, Feedback.run_id == Run.id)
    if scope is not None:
        stmt = stmt.where(scope)
    if rating:
        stmt = stmt.where(Feedback.rating == rating)
    rows = (await session.execute(stmt.order_by(Feedback.created_at.desc()).limit(200))).scalars().all()
    return [_feedback_out(row) for row in rows]


@router.post("/feedback/{feedback_id}/convert", status_code=201)
async def convert_feedback(feedback_id: str, payload: FeedbackConvert, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    scope = await _run_scope(request, session)
    stmt = select(Feedback).join(Run, Feedback.run_id == Run.id).where(Feedback.id == feedback_id)
    if scope is not None:
        stmt = stmt.where(scope)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "feedback not found")
    if row.rating != "negative":
        raise HTTPException(400, "only negative feedback can become a regression case")
    if row.eval_case_id:
        raise HTTPException(409, "feedback has already been converted")
    context = _json(row.context_snapshot, {})
    source_case_id = None
    source_suite_id = None
    gate_rows = (await session.execute(
        select(GateRun).join(AgentVersion, GateRun.agent_version_id == AgentVersion.id)
        .where(AgentVersion.agent_id == row.agent_id)
    )).scalars().all()
    for gate in gate_rows:
        for evidence in _json(gate.evidence, []) or []:
            if evidence.get("run_id") == row.run_id:
                source_case_id = evidence.get("case_id")
                source_suite_id = gate.suite_id
                break
        if source_case_id:
            break
    case = await session.get(EvalCase, source_case_id) if source_case_id else None
    if case is None:
        case = EvalCase(
            id=str(uuid.uuid4()), agent_id=row.agent_id, owner_id=row.created_by, team_id=row.team_id,
            dataset="feedback", name=f"feedback-{row.id[:8]}", input=str(context.get("input") or ""),
            expected=payload.expected_answer, meta=json.dumps({"source_feedback_id": row.id,
            "agent_version_id": row.agent_version_id}, ensure_ascii=False),
            tool_calls="[]", score_details="{}",
        )
        session.add(case)
    else:
        # Old Gate conditions remain immutable; only future suites see the correction.
        case.expected = payload.expected_answer
    row.eval_case_id = case.id
    suite_id = payload.suite_id or source_suite_id
    if suite_id:
        suite = await session.get(EvalSuite, suite_id)
        if suite is None or suite.team_id != row.team_id:
            raise HTTPException(404, "suite not found in this team")
        if suite.agent_id is not None and suite.agent_id != row.agent_id:
            raise HTTPException(400, "suite belongs to a different agent")
        case_ids = _json(suite.case_ids, [])
        if case.id not in case_ids:
            case_ids.append(case.id)
        suite.case_ids = json.dumps(case_ids, ensure_ascii=False)
        row.suite_id = suite.id
    record_audit(session, request, action="feedback.convert", target_type="eval_case", target_id=case.id,
                 team_id=row.team_id, detail={"feedback_id": row.id, "suite_id": row.suite_id})
    await session.commit()
    return {"feedback": _feedback_out(row), "eval_case_id": case.id, "suite_id": row.suite_id}


async def _scoped_feedback(
    feedback_id: str, request: Request, session: AsyncSession,
) -> Feedback:
    scope = await _run_scope(request, session)
    stmt = select(Feedback).join(Run, Feedback.run_id == Run.id).where(Feedback.id == feedback_id)
    if scope is not None:
        stmt = stmt.where(scope)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "feedback not found")
    return row


def _resolution_out(row: FeedbackResolution) -> dict[str, Any]:
    return {
        "id": row.id, "feedback_id": row.feedback_id, "gate_run_id": row.gate_run_id,
        "case_id": row.case_id, "verdict": row.verdict, "note": row.note,
        "created_by": row.created_by, "created_at": row.created_at,
    }


@router.get("/feedback/{feedback_id}/resolutions")
async def list_feedback_resolutions(
    feedback_id: str, request: Request, session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    row = await _scoped_feedback(feedback_id, request, session)
    rows = (await session.execute(
        select(FeedbackResolution).where(FeedbackResolution.feedback_id == row.id)
        .order_by(FeedbackResolution.created_at.asc())
    )).scalars().all()
    return [_resolution_out(item) for item in rows]


@router.post("/feedback/{feedback_id}/resolutions", status_code=201)
async def create_feedback_resolution(
    feedback_id: str, payload: FeedbackResolutionCreate, request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    feedback = await _scoped_feedback(feedback_id, request, session)
    principal = get_principal(request)
    if principal.role != "admin":
        if not feedback.team_id:
            raise HTTPException(404, "feedback not found")
        await authorize(request, session, "approval:manage", feedback.team_id)
    gate = await session.get(GateRun, payload.gate_run_id)
    version = await session.get(AgentVersion, gate.agent_version_id) if gate else None
    if (
        gate is None or version is None or version.agent_id != feedback.agent_id
        or version.team_id != feedback.team_id
    ):
        raise HTTPException(404, "gate run not found for this feedback")
    if feedback.suite_id is not None and gate.suite_id != feedback.suite_id:
        raise HTTPException(422, "resolution GateRun does not match the feedback suite")
    if payload.verdict == "confirmed_fixed" and feedback.eval_case_id is None:
        raise HTTPException(422, "confirmed_fixed requires a feedback regression case")
    actor = principal.user_id or "local-single-user"
    try:
        gate_record = GateRecordV1.model_validate({
            "id": gate.id, "agent_version_id": gate.agent_version_id, "suite_id": gate.suite_id,
            "baseline_gate_id": gate.baseline_gate_id, "status": gate.status,
            "quality_record_version": gate.quality_record_version,
            "conditions": _json(gate.conditions, None), "details": _json(gate.details, []),
            "evidence": _json(gate.evidence, None), "diff": _json(gate.diff, []),
        })
        feedback_record = FeedbackRecord.model_validate({
            "id": feedback.id, "run_id": feedback.run_id, "agent_id": feedback.agent_id,
            "agent_version_id": feedback.agent_version_id, "rating": feedback.rating,
            "expected_answer": feedback.expected_answer, "eval_case_id": feedback.eval_case_id,
            "suite_id": feedback.suite_id,
        })
        resolution = FeedbackResolutionContract.model_validate({
            "id": str(uuid.uuid4()), "feedback_id": feedback.id, "gate_run_id": gate.id,
            "case_id": payload.case_id, "verdict": payload.verdict, "note": payload.note,
            "created_by": actor, "created_at": datetime.now(UTC),
        })
        validate_feedback_resolution(
            feedback_record, resolution, gate_record, human_reviewed=True,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    row = FeedbackResolution(
        id=str(resolution.id), feedback_id=feedback.id, gate_run_id=gate.id,
        case_id=payload.case_id, verdict=payload.verdict, note=payload.note,
        created_by=actor, created_at=resolution.created_at.replace(tzinfo=None),
    )
    session.add(row)
    await session.flush()
    record_audit(
        session, request, action="feedback.resolve", target_type="feedback_resolution",
        target_id=row.id, team_id=feedback.team_id,
        detail={"feedback_id": feedback.id, "gate_run_id": gate.id, "verdict": row.verdict},
    )
    await session.commit()
    return _resolution_out(row)


def _rule_out(row: AlertRule) -> dict[str, Any]:
    return {"id": row.id, "team_id": row.team_id, "name": row.name, "kind": row.kind,
            "threshold": row.threshold, "enabled": row.enabled, "created_at": row.created_at}


def _event_out(row: AlertEvent) -> dict[str, Any]:
    return {"id": row.id, "rule_id": row.rule_id, "team_id": row.team_id, "status": row.status,
            "message": row.message, "detail": _json(row.detail, {}), "created_at": row.created_at,
            "acknowledged_by": row.acknowledged_by, "acknowledged_at": row.acknowledged_at}


@router.get("/alerts/rules")
async def list_rules(request: Request, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(AlertRule).order_by(AlertRule.created_at.desc())
    teams = await accessible_team_ids(session, request)
    if teams is not None:
        stmt = stmt.where(AlertRule.team_id.in_(teams)) if teams else stmt.where(AlertRule.id.is_(None))
    return [_rule_out(row) for row in (await session.execute(stmt)).scalars().all()]


@router.post("/alerts/rules", status_code=201)
async def create_rule(payload: AlertRuleCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if get_principal(request).role != "admin":
        if not payload.team_id:
            raise HTTPException(400, "team_id is required")
        await authorize(request, session, "approval:manage", payload.team_id)
    row = AlertRule(**payload.model_dump(), created_by=get_principal(request).user_id)
    session.add(row)
    await session.flush()
    record_audit(session, request, action="alert_rule.create", target_type="alert_rule", target_id=row.id,
                 team_id=row.team_id, detail={"kind": row.kind, "threshold": row.threshold})
    await session.commit()
    return _rule_out(row)


@router.get("/alerts/events")
async def list_events(request: Request, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(200)
    teams = await accessible_team_ids(session, request)
    if teams is not None:
        stmt = stmt.where(AlertEvent.team_id.in_(teams)) if teams else stmt.where(AlertEvent.id.is_(None))
    return [_event_out(row) for row in (await session.execute(stmt)).scalars().all()]


@router.post("/alerts/evaluate", dependencies=[Depends(require_admin)])
async def evaluate_alerts(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    events = await evaluate_alert_rules(session)
    return {"created": len(events), "events": [_event_out(row) for row in events]}


@router.post("/alerts/events/{event_id}/ack")
async def acknowledge_event(event_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await session.get(AlertEvent, event_id)
    if row is None:
        raise HTTPException(404, "alert event not found")
    if get_principal(request).role != "admin":
        if not row.team_id:
            raise HTTPException(404, "alert event not found")
        await authorize(request, session, "approval:manage", row.team_id)
    row.status = "acknowledged"
    row.acknowledged_by = get_principal(request).user_id
    row.acknowledged_at = datetime.utcnow()
    await session.commit()
    return _event_out(row)


__all__ = ["router"]

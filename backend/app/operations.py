"""Operations aggregation, historical cost accounting, and alert evaluation."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, AgentVersion, AlertEvent, AlertRule, GateRun, ModelPrice, Run

SUCCESS = {"success", "completed", "ok"}
FAILED = {"failed", "error", "terminated"}


def _config(run: Run) -> dict[str, Any]:
    try:
        value = json.loads(run.config or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _model(run: Run, agents: dict[str, Agent]) -> str:
    return str(_config(run).get("model") or getattr(agents.get(run.agent_id), "model", "unknown"))


def _provider(run: Run, agents: dict[str, Agent]) -> str:
    return str(getattr(agents.get(run.agent_id), "provider", "unknown"))


def _bucket(value: datetime, group: str) -> str:
    if group == "hour":
        point = value.replace(minute=0, second=0, microsecond=0)
    elif group == "week":
        point = (value - timedelta(days=value.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        point = value.replace(hour=0, minute=0, second=0, microsecond=0)
    return point.isoformat()


def _rejected(run: Run) -> bool:
    text = str(run.error or "").lower()
    return any(word in text for word in ("denied", "permission", "rejected", "blocked", "sandbox"))


def _summary(runs: list[Run]) -> dict[str, Any]:
    durations = sorted(int(run.duration_ms or 0) for run in runs)
    failed = sum(run.status in FAILED for run in runs)
    rejected = sum(_rejected(run) for run in runs)
    errors: dict[str, int] = defaultdict(int)
    for run in runs:
        if run.status in FAILED:
            errors[str(run.error or "run_failed").split(":", 1)[0][:100]] += 1
    return {
        "requests": len(runs),
        "success": sum(run.status in SUCCESS for run in runs),
        "failed": failed,
        "rejected": rejected,
        "failure_rate": failed / len(runs) if runs else 0.0,
        "prompt_tokens": sum(int(run.prompt_tokens or 0) for run in runs),
        "completion_tokens": sum(int(run.completion_tokens or 0) for run in runs),
        "total_tokens": sum(int(run.total_tokens or 0) for run in runs),
        "average_duration_ms": sum(durations) / len(durations) if durations else 0.0,
        "p95_duration_ms": durations[min(len(durations) - 1, int(len(durations) * 0.95))] if durations else 0,
        "errors": dict(errors),
    }


def aggregate_metrics(runs: list[Run], agents: dict[str, Agent], group: str = "day") -> dict[str, Any]:
    if group not in {"hour", "day", "week"}:
        raise ValueError("group must be hour, day, or week")
    trends: dict[str, list[Run]] = defaultdict(list)
    dimensions: dict[str, dict[str, list[Run]]] = {
        "agents": defaultdict(list), "teams": defaultdict(list), "models": defaultdict(list),
    }
    for run in runs:
        trends[_bucket(run.started_at, group)].append(run)
        dimensions["agents"][run.agent_id].append(run)
        dimensions["teams"][run.team_id or "personal"].append(run)
        dimensions["models"][_model(run, agents)].append(run)
    return {
        "summary": _summary(runs),
        "trends": [{"bucket": key, **_summary(values)} for key, values in sorted(trends.items())],
        "dimensions": {name: {key: _summary(values) for key, values in sorted(groups.items())}
                       for name, groups in dimensions.items()},
    }


def _price(run: Run, agents: dict[str, Agent], prices: list[ModelPrice]) -> ModelPrice | None:
    provider, model = _provider(run, agents), _model(run, agents)
    candidates = [row for row in prices if row.provider == provider and row.model == model and row.effective_from <= run.started_at]
    if not candidates:
        return None
    return max(candidates, key=lambda row: row.effective_from)


def calculate_costs(runs: list[Run], agents: dict[str, Agent], prices: list[ModelPrice]) -> dict[str, Any]:
    total = Decimal("0")
    unpriced = 0
    currencies: set[str] = set()
    agents_cost: dict[str, Decimal] = defaultdict(Decimal)
    models_cost: dict[str, Decimal] = defaultdict(Decimal)
    daily: dict[str, Decimal] = defaultdict(Decimal)
    for run in runs:
        price = _price(run, agents, prices)
        if price is None:
            unpriced += int(run.total_tokens or 0)
            continue
        currencies.add(price.currency)
        cost = ((Decimal(int(run.prompt_tokens or 0)) * Decimal(str(price.input_per_million))
                 + Decimal(int(run.completion_tokens or 0)) * Decimal(str(price.output_per_million)))
                / Decimal(1_000_000))
        total += cost
        agents_cost[run.agent_id] += cost
        models_cost[f"{price.provider}/{price.model}"] += cost
        daily[_bucket(run.started_at, "day")] += cost
    currency = currencies.pop() if len(currencies) == 1 else "MIXED"
    return {
        "total": float(total), "currency": currency, "unpriced_tokens": unpriced,
        "top_agents": [{"agent_id": key, "cost": float(value)} for key, value in
                       sorted(agents_cost.items(), key=lambda item: item[1], reverse=True)],
        "models": [{"model": key, "cost": float(value)} for key, value in
                   sorted(models_cost.items(), key=lambda item: item[1], reverse=True)],
        "trends": [{"bucket": key, "cost": float(value)} for key, value in sorted(daily.items())],
    }


async def evaluate_alert_rules(session: AsyncSession, now: datetime | None = None) -> list[AlertEvent]:
    """Evaluate enabled rules once and create deduplicated in-app notifications."""
    current = now or datetime.utcnow()
    rules = (await session.execute(select(AlertRule).where(AlertRule.enabled.is_(True)))).scalars().all()
    prices = (await session.execute(select(ModelPrice))).scalars().all()
    agents = {row.id: row for row in (await session.execute(select(Agent))).scalars().all()}
    created: list[AlertEvent] = []
    for rule in rules:
        triggered = False
        detail: dict[str, Any] = {}
        stmt = select(Run)
        if rule.team_id:
            stmt = stmt.where(Run.team_id == rule.team_id)
        if rule.kind == "failure_rate":
            runs = (await session.execute(stmt.where(Run.started_at >= current - timedelta(hours=24)))).scalars().all()
            summary = _summary(list(runs))
            triggered = bool(runs) and summary["failure_rate"] > rule.threshold
            detail = {"window": "24h", **summary}
        elif rule.kind == "daily_cost":
            start = current.replace(hour=0, minute=0, second=0, microsecond=0)
            runs = (await session.execute(stmt.where(Run.started_at >= start))).scalars().all()
            cost = calculate_costs(list(runs), agents, list(prices))
            triggered = cost["total"] > rule.threshold
            detail = cost
        elif rule.kind == "error_spike":
            hour_start = current.replace(minute=0, second=0, microsecond=0)
            recent = list((await session.execute(stmt.where(Run.started_at >= hour_start))).scalars().all())
            baseline = list((await session.execute(stmt.where(
                Run.started_at >= hour_start - timedelta(hours=24), Run.started_at < hour_start
            ))).scalars().all())
            recent_rate, baseline_rate = _summary(recent)["failure_rate"], _summary(baseline)["failure_rate"]
            triggered = bool(recent and baseline) and recent_rate > 0 and recent_rate >= baseline_rate * rule.threshold
            detail = {"recent_failure_rate": recent_rate, "baseline_failure_rate": baseline_rate,
                      "recent_requests": len(recent), "baseline_requests": len(baseline)}
        elif rule.kind == "gate_failures":
            gate_stmt = select(GateRun).join(AgentVersion, GateRun.agent_version_id == AgentVersion.id)
            if rule.team_id:
                gate_stmt = gate_stmt.where(AgentVersion.team_id == rule.team_id)
            gates = (await session.execute(gate_stmt.order_by(GateRun.created_at.desc()).limit(max(1, int(rule.threshold))))).scalars().all()
            triggered = len(gates) >= max(1, int(rule.threshold)) and all(row.status == "failed" for row in gates)
            detail = {"gate_run_ids": [row.id for row in gates], "consecutive": len(gates)}
        else:
            continue
        if not triggered:
            continue
        existing = await session.scalar(select(AlertEvent.id).where(
            AlertEvent.rule_id == rule.id, AlertEvent.status == "open"
        ))
        if existing:
            continue
        event = AlertEvent(
            rule_id=rule.id, team_id=rule.team_id, status="open",
            message=f"{rule.name}: {rule.kind} exceeded threshold {rule.threshold}",
            detail=json.dumps(detail, ensure_ascii=False),
        )
        session.add(event)
        created.append(event)
    await session.commit()
    return created


__all__ = ["aggregate_metrics", "calculate_costs", "evaluate_alert_rules"]

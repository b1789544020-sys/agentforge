"""Atomic canonical Agent package preflight and write plans."""
from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.definitions import (
    ResolvedDefinition,
    apply_definition,
    resolve_definition,
    snapshot_dict,
)
from app.agent.schema import AgentDefinition, ResourceMap
from app.agent.versions import create_agent_version, latest_agent_version
from app.models import Agent, AgentVersion


class AgentNameConflictError(ValueError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__("agent name already exists")


@dataclass
class CanonicalImportPlan:
    definition: AgentDefinition
    agent: Agent
    action: str
    resolved: ResolvedDefinition | None = None


@dataclass
class CanonicalPreflight:
    plans: list[CanonicalImportPlan] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)


def _config(agent: Agent | None) -> dict[str, Any]:
    if agent is None:
        return {}
    try:
        value = json.loads(agent.config or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _new_agent(definition: AgentDefinition, owner_id: str | None, team_id: str | None) -> Agent:
    return Agent(
        id=str(uuid.uuid4()), owner_id=owner_id, team_id=team_id,
        name=definition.name, description=definition.description,
        system_prompt=definition.system_prompt, provider=definition.provider,
        model=definition.model, temperature=definition.temperature,
        max_steps=definition.max_steps, tools="[]", config="{}",
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )


async def preflight_canonical_import(
    session: AsyncSession,
    rows: list[dict[str, Any]],
    *,
    on_conflict: str,
    owner_id: str | None,
    team_id: str | None,
    scope: Any,
    resource_map: ResourceMap,
    secret_inputs: Mapping[str, str | None],
) -> CanonicalPreflight:
    output = CanonicalPreflight()
    seen_names: set[str] = set()
    active_refs: set[str] = set()
    active_kb_refs: set[str] = set()
    active_prompt_names: set[str] = set()
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    inherited: list[dict[str, Any]] = []

    for raw in rows:
        definition = AgentDefinition.model_validate(raw)
        if definition.name in seen_names:
            raise ValueError("agent names must be unique across a canonical package")
        seen_names.add(definition.name)
        stmt = select(Agent).where(Agent.name == definition.name)
        if scope is not None:
            stmt = stmt.where(scope)
        existing = (await session.execute(stmt)).scalars().first()
        if existing is not None and on_conflict == "error":
            raise AgentNameConflictError(definition.name)
        action = "create" if existing is None else ("skip" if on_conflict == "skip" else "update")
        agent = existing or _new_agent(definition, owner_id, team_id)
        plan = CanonicalImportPlan(definition=definition, agent=agent, action=action)
        output.plans.append(plan)
        if action == "skip":
            continue
        row_refs = {row.ref for row in definition.secret_refs}
        if active_refs & row_refs:
            raise ValueError("secret ref values must be unique across a canonical package")
        active_refs.update(row_refs)
        row_kb_refs = {row.ref for row in definition.resources.knowledge_bases}
        if active_kb_refs & row_kb_refs:
            raise ValueError("knowledge base ref values must be unique across a canonical package")
        active_kb_refs.update(row_kb_refs)
        if definition.resources.prompt is not None:
            active_prompt_names.add(definition.resources.prompt.name)
            active_prompt_names.add(f"{definition.name}:{definition.resources.prompt.name}")
        previous = await latest_agent_version(session, agent.id) if existing is not None else None
        plan.resolved = await resolve_definition(
            session, agent, definition, resource_map=resource_map,
            secret_inputs={key: value for key, value in secret_inputs.items() if key in row_refs},
            previous_config=_config(existing), previous_snapshot=snapshot_dict(previous),
            existing_agent=existing is not None,
        )
        resolved.extend(plan.resolved.resolved)
        unresolved.extend(plan.resolved.unresolved)
        unsupported.extend(plan.resolved.unsupported_tools)
        missing.extend({"agent": definition.name, **row} for row in plan.resolved.missing_secrets)
        inherited.extend({"agent": definition.name, **row} for row in plan.resolved.inherited_secrets)

    unknown_inputs = sorted(set(secret_inputs) - active_refs)
    if unknown_inputs:
        raise ValueError(f"secret_inputs contains unknown refs: {', '.join(unknown_inputs)}")
    unknown_kb = sorted(set(resource_map.knowledge_bases) - active_kb_refs)
    unknown_prompts = sorted(set(resource_map.prompts) - active_prompt_names)
    if unknown_kb or unknown_prompts:
        raise ValueError("resource_map contains entries not referenced by the package")
    created = [plan.definition.name for plan in output.plans if plan.action == "create"]
    updated = [plan.definition.name for plan in output.plans if plan.action == "update"]
    skipped = [plan.definition.name for plan in output.plans if plan.action == "skip"]
    output.report = {
        "ok": not unresolved and not unsupported,
        "format": None, "on_conflict": on_conflict,
        "imported": len(created), "updated": len(updated), "skipped": len(skipped),
        "created": created, "updated_list": updated, "skipped_list": skipped,
        "resolved": resolved, "unresolved": unresolved, "unsupported_tools": unsupported,
        "missing_secrets": missing, "inherited_secrets": inherited,
    }
    return output


async def write_canonical_import(
    session: AsyncSession, preflight: CanonicalPreflight, *, created_by: str | None,
) -> list[tuple[CanonicalImportPlan, AgentVersion]]:
    if not preflight.report.get("ok"):
        raise ValueError("canonical import preflight failed")
    written: list[tuple[CanonicalImportPlan, AgentVersion]] = []
    for plan in preflight.plans:
        if plan.action == "skip":
            continue
        assert plan.resolved is not None
        if plan.action == "create":
            session.add(plan.agent)
            await session.flush()
        await apply_definition(session, plan.agent, plan.resolved)
        plan.agent.updated_at = datetime.utcnow()
        version = await create_agent_version(session, plan.agent, created_by=created_by)
        written.append((plan, version))
    return written


__all__ = [
    "AgentNameConflictError", "CanonicalImportPlan", "CanonicalPreflight",
    "preflight_canonical_import", "write_canonical_import",
]

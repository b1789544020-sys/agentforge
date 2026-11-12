"""Immutable Agent version snapshots and runtime reconstruction."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schema import runtime_secret_refs
from app.models import (
    Agent,
    AgentVersion,
    KnowledgeBase,
    KnowledgeBinding,
    PromptTemplate,
    PromptVersion,
)


def _object(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except (TypeError, ValueError):
        return fallback


def runtime_snapshot(agent: Agent) -> dict[str, Any]:
    """Capture every field needed to reproduce this Agent configuration."""
    return {
        "name": agent.name,
        "team_id": agent.team_id,
        "description": agent.description,
        "system_prompt": agent.system_prompt,
        "model": agent.model,
        "provider": agent.provider,
        "temperature": float(agent.temperature),
        "max_steps": int(agent.max_steps),
        "tools": _object(agent.tools, []),
        "config": _object(agent.config, {}),
        "memory": _object(agent.config, {}).get("memory", {}),
    }


async def _runtime_resources(
    session: AsyncSession,
    agent: Agent,
    snapshot: dict[str, Any],
    previous_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pin mutable resource rows into a self-contained runtime snapshot."""
    config = snapshot["config"]
    prompt = None
    template_id = config.get("prompt_template_id")
    if template_id:
        template = await session.get(PromptTemplate, template_id)
        if template is None or template.agent_id != agent.id:
            raise ValueError("bound prompt template does not belong to this agent")
        stmt = select(PromptVersion).where(PromptVersion.template_id == template_id)
        requested_version = config.get("prompt_version")
        if requested_version is None:
            stmt = stmt.order_by(PromptVersion.version.desc()).limit(1)
        else:
            stmt = stmt.where(PromptVersion.version == int(requested_version))
        prompt_version = (await session.execute(stmt)).scalars().first()
        if prompt_version is None:
            raise ValueError(f"prompt version {requested_version!r} was not found")
        prompt = {
            "template_id": template.id,
            "name": template.name,
            "version": prompt_version.version,
            "content": prompt_version.content,
            "content_sha256": hashlib.sha256(prompt_version.content.encode("utf-8")).hexdigest(),
        }

    bound_ids = set((await session.execute(
        select(KnowledgeBinding.kb_id).where(KnowledgeBinding.agent_id == agent.id)
    )).scalars().all())
    configured_ids = config.get("knowledge_base_ids") or []
    if not isinstance(configured_ids, list):
        raise ValueError("config.knowledge_base_ids must be a list")
    kb_ids = sorted(bound_ids | {str(item) for item in configured_ids})
    knowledge = []
    previous_resources = (previous_snapshot or {}).get("resources") or {}
    previous_knowledge = previous_resources.get("knowledge_bases") or []
    previous_refs = {
        str(row.get("kb_id") or row.get("id")): str(row["ref"])
        for row in previous_knowledge
        if isinstance(row, dict) and row.get("ref") and (row.get("kb_id") or row.get("id"))
    }
    previous_ref_values = {
        row.get("ref") for row in previous_knowledge
        if isinstance(row, dict) and row.get("ref")
    }
    if len(previous_refs) != len(previous_ref_values):
        raise ValueError("previous knowledge binding refs are ambiguous")
    used_refs: set[str] = set()
    explicit_refs = getattr(agent, "_schema_knowledge_refs", {})
    reserved_refs = set(previous_ref_values) | set(explicit_refs.values())

    def next_ref() -> str:
        index = 1
        while f"kb-{index}" in used_refs or f"kb-{index}" in reserved_refs:
            index += 1
        return f"kb-{index}"

    if kb_ids:
        rows = (await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids))
        )).scalars().all()
        by_id = {row.id: row for row in rows}
        missing = set(kb_ids) - set(by_id)
        if missing:
            raise ValueError("knowledge_base_ids contains an unavailable resource")
        for kb_id in kb_ids:
            row = by_id[kb_id]
            if row.team_id != agent.team_id:
                raise ValueError("knowledge base does not belong to the agent team")
            if (
                row.team_id is None and row.owner_id is not None
                and row.owner_id != agent.owner_id
            ):
                raise ValueError("knowledge base does not belong to the agent owner")
            ref = explicit_refs.get(kb_id) or previous_refs.get(kb_id) or next_ref()
            if ref in used_refs:
                raise ValueError("knowledge binding refs must be unique")
            used_refs.add(ref)
            knowledge.append({"kb_id": row.id, "ref": ref, "name": row.name})
    knowledge.sort(key=lambda row: row["ref"])
    return {"prompt": prompt, "knowledge_bases": knowledge}


async def latest_agent_version(
    session: AsyncSession, agent_id: str, *, lock: bool = False
) -> AgentVersion | None:
    stmt = (
        select(AgentVersion).where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version_number.desc()).limit(1)
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalars().first()


async def create_agent_version(
    session: AsyncSession, agent: Agent, *, created_by: str | None = None
) -> AgentVersion:
    """Append a snapshot; PostgreSQL locks the parent to serialize numbering."""
    await session.execute(
        select(Agent.id).where(Agent.id == agent.id).with_for_update()
    )
    previous = await latest_agent_version(session, agent.id)
    latest = previous.version_number if previous is not None else 0
    previous_snapshot = _object(previous.snapshot, {}) if previous is not None else {}
    snapshot = runtime_snapshot(agent)
    snapshot["schema_version"] = 1
    snapshot["owner_id"] = agent.owner_id
    snapshot["resources"] = await _runtime_resources(session, agent, snapshot, previous_snapshot)
    if hasattr(agent, "_schema_secret_refs"):
        snapshot["secret_refs"] = copy.deepcopy(agent._schema_secret_refs)
    else:
        snapshot["secret_refs"] = runtime_secret_refs(
            snapshot["config"], previous_snapshot.get("secret_refs") or []
        )
    if hasattr(agent, "_schema_extensions"):
        snapshot["extensions"] = copy.deepcopy(agent._schema_extensions)
    else:
        snapshot["extensions"] = copy.deepcopy(previous_snapshot.get("extensions") or {})
    version = AgentVersion(
        agent_id=agent.id,
        team_id=agent.team_id,
        version_number=int(latest or 0) + 1,
        snapshot=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
        system_prompt=agent.system_prompt,
        model=agent.model,
        provider=agent.provider,
        temperature=agent.temperature,
        max_steps=agent.max_steps,
        tools=json.dumps(snapshot["tools"], ensure_ascii=False),
        config=json.dumps(snapshot["config"], ensure_ascii=False),
        gate_status="pending",
        created_by=created_by,
    )
    session.add(version)
    await session.flush()
    return version


def agent_from_version(version: AgentVersion) -> Agent:
    """Build a transient Agent without consulting its mutable current row."""
    snapshot = _object(version.snapshot, {})
    if not isinstance(snapshot, dict) or not snapshot:
        raise ValueError("agent version snapshot is missing or invalid")
    agent = Agent(
        id=version.agent_id,
        owner_id=snapshot.get("owner_id"),
        team_id=version.team_id or snapshot.get("team_id"),
        name=str(snapshot.get("name") or f"agent-{version.agent_id[:8]}"),
        description=snapshot.get("description"),
        system_prompt=snapshot.get("system_prompt"),
        model=str(snapshot["model"]),
        provider=str(snapshot["provider"]),
        temperature=float(snapshot["temperature"]),
        max_steps=int(snapshot["max_steps"]),
        tools=json.dumps(snapshot.get("tools") or [], ensure_ascii=False),
        config=json.dumps(snapshot.get("config") or {}, ensure_ascii=False),
    )
    agent._agent_version_id = version.id
    if snapshot.get("schema_version") == 1:
        resources = snapshot.get("resources") or {}
        knowledge = resources.get("knowledge_bases") or []
        config = snapshot.get("config") or {}
        config["knowledge_base_ids"] = sorted({
            str(row["kb_id"]) for row in knowledge if isinstance(row, dict) and row.get("kb_id")
        })
        agent.config = json.dumps(config, ensure_ascii=False)
        agent._runtime_snapshot = snapshot
    return agent


__all__ = ["agent_from_version", "create_agent_version", "latest_agent_version", "runtime_snapshot"]

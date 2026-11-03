"""Immutable Agent version snapshots and runtime reconstruction."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, AgentVersion


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


async def create_agent_version(
    session: AsyncSession, agent: Agent, *, created_by: str | None = None
) -> AgentVersion:
    """Append a snapshot; PostgreSQL locks the parent to serialize numbering."""
    await session.execute(
        select(Agent.id).where(Agent.id == agent.id).with_for_update()
    )
    latest = await session.scalar(
        select(func.max(AgentVersion.version_number)).where(AgentVersion.agent_id == agent.id)
    )
    snapshot = runtime_snapshot(agent)
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
    return Agent(
        id=version.agent_id,
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


__all__ = ["runtime_snapshot", "create_agent_version", "agent_from_version"]

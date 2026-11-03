"""Built-in/custom Agent template catalog and instantiation API."""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.versions import create_agent_version, runtime_snapshot
from app.api.agents import _serialize_agent
from app.api.auth import get_principal
from app.audit import record_audit
from app.database import get_session
from app.dispatch import submit_gate
from app.gates.service import create_automatic_gates
from app.models import Agent, AgentTemplate, AgentTemplateVersion, EvalCase
from app.rbac import accessible_team_ids, resolve_team_for_create, scoped_agent_ids
from app.secrets import protect_config, redact_config
from app.templates import BUILTIN_TEMPLATES

router = APIRouter(prefix="/templates", tags=["templates"])


class TemplateCreate(BaseModel):
    agent_id: str
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    category: str = "custom"


class TemplateEdit(BaseModel):
    snapshot: dict[str, Any]
    description: str | None = None
    category: str | None = None


class TemplateInstantiate(BaseModel):
    name: str | None = None
    team_id: str | None = None


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except (TypeError, ValueError):
        return fallback


def _version_out(row: AgentTemplateVersion) -> dict[str, Any]:
    return {"id": row.id, "template_id": row.template_id, "version_number": row.version_number,
            "snapshot": redact_config(_json(row.snapshot, {})), "created_by": row.created_by,
            "created_at": row.created_at}


def _custom_out(template: AgentTemplate, version: AgentTemplateVersion) -> dict[str, Any]:
    return {"id": template.id, "name": template.name, "description": template.description,
            "category": template.category, "team_id": template.team_id, "builtin": False,
            "version_number": version.version_number, "snapshot": redact_config(_json(version.snapshot, {})),
            "examples": _json(version.snapshot, {}).get("examples", [])}


async def _latest(session: AsyncSession, template_id: str) -> AgentTemplateVersion | None:
    return (await session.execute(select(AgentTemplateVersion).where(
        AgentTemplateVersion.template_id == template_id
    ).order_by(AgentTemplateVersion.version_number.desc()).limit(1))).scalars().first()


@router.get("")
async def list_templates(request: Request, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = [*BUILTIN_TEMPLATES]
    teams = await accessible_team_ids(session, request)
    stmt = select(AgentTemplate).order_by(AgentTemplate.created_at.desc())
    if teams is not None:
        stmt = stmt.where(AgentTemplate.team_id.in_(teams)) if teams else stmt.where(
            AgentTemplate.owner_id == get_principal(request).user_id
        )
    for template in (await session.execute(stmt)).scalars().all():
        version = await _latest(session, template.id)
        if version:
            rows.append(_custom_out(template, version))
    return rows


@router.post("", status_code=201)
async def save_template(payload: TemplateCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    agent = await session.get(Agent, payload.agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    if get_principal(request).role != "admin" and agent.id not in (await scoped_agent_ids(session, request) or set()):
        raise HTTPException(404, "agent not found")
    snapshot = runtime_snapshot(agent)
    snapshot["examples"] = []
    template = AgentTemplate(
        team_id=agent.team_id, owner_id=get_principal(request).user_id,
        name=payload.name.strip(), description=payload.description, category=payload.category,
    )
    session.add(template)
    await session.flush()
    version = AgentTemplateVersion(
        template_id=template.id, version_number=1, snapshot=json.dumps(snapshot, ensure_ascii=False),
        created_by=get_principal(request).user_id,
    )
    session.add(version)
    record_audit(session, request, action="template.create", target_type="template", target_id=template.id,
                 team_id=template.team_id, detail={"agent_id": agent.id})
    await session.commit()
    return _custom_out(template, version)


@router.get("/{template_id}/versions")
async def template_versions(template_id: str, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = (await session.execute(select(AgentTemplateVersion).where(
        AgentTemplateVersion.template_id == template_id
    ).order_by(AgentTemplateVersion.version_number.desc()))).scalars().all()
    if not rows:
        raise HTTPException(404, "template not found or built-in has only version 1")
    return [_version_out(row) for row in rows]


@router.post("/{template_id}/versions", status_code=201)
async def edit_template(template_id: str, payload: TemplateEdit, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    template = await session.get(AgentTemplate, template_id)
    if template is None:
        raise HTTPException(404, "custom template not found; built-ins must be copied first")
    if get_principal(request).role != "admin":
        teams = await accessible_team_ids(session, request) or set()
        if template.team_id and template.team_id not in teams:
            raise HTTPException(404, "template not found")
        if not template.team_id and template.owner_id != get_principal(request).user_id:
            raise HTTPException(404, "template not found")
    latest = await _latest(session, template_id)
    snapshot = dict(payload.snapshot)
    snapshot["config"] = protect_config(snapshot.get("config") or {})
    version = AgentTemplateVersion(
        template_id=template_id, version_number=(latest.version_number if latest else 0) + 1,
        snapshot=json.dumps(snapshot, ensure_ascii=False), created_by=get_principal(request).user_id,
    )
    if payload.description is not None:
        template.description = payload.description
    if payload.category:
        template.category = payload.category
    session.add(version)
    record_audit(session, request, action="template.version", target_type="template", target_id=template.id,
                 team_id=template.team_id, detail={"version_number": version.version_number})
    await session.commit()
    return _custom_out(template, version)


@router.post("/{template_id}/instantiate", status_code=201)
async def instantiate(template_id: str, payload: TemplateInstantiate, background: BackgroundTasks, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    builtin = next((row for row in BUILTIN_TEMPLATES if row["id"] == template_id), None)
    template = None if builtin else await session.get(AgentTemplate, template_id)
    latest = None if builtin else await _latest(session, template_id)
    if builtin is None and (template is None or latest is None):
        raise HTTPException(404, "template not found")
    if template and get_principal(request).role != "admin":
        teams = await accessible_team_ids(session, request) or set()
        if template.team_id and template.team_id not in teams:
            raise HTTPException(404, "template not found")
        if not template.team_id and template.owner_id != get_principal(request).user_id:
            raise HTTPException(404, "template not found")
    snapshot = builtin["snapshot"] if builtin else _json(latest.snapshot, {})
    examples = builtin["examples"] if builtin else snapshot.get("examples", [])
    team_id = await resolve_team_for_create(request, session, payload.team_id)
    agent = Agent(
        id=str(uuid.uuid4()), owner_id=get_principal(request).user_id, team_id=team_id,
        name=payload.name or snapshot.get("name") or f"Agent from {template_id}",
        description=snapshot.get("description"), system_prompt=snapshot.get("system_prompt"),
        model=snapshot.get("model") or "mock-model", provider=snapshot.get("provider") or "mock",
        temperature=snapshot.get("temperature", 0.7), max_steps=snapshot.get("max_steps", 10),
        tools=json.dumps(snapshot.get("tools") or [], ensure_ascii=False),
        config=json.dumps(protect_config(snapshot.get("config") or {}), ensure_ascii=False),
    )
    session.add(agent)
    await session.flush()
    version = await create_agent_version(session, agent, created_by=get_principal(request).user_id)
    case_ids: list[str] = []
    for index, example in enumerate(examples):
        case_id = str(uuid.uuid4())
        case_ids.append(case_id)
        session.add(EvalCase(
            id=case_id, agent_id=agent.id, owner_id=agent.owner_id, team_id=agent.team_id,
            dataset=f"template:{template_id}", name=f"example-{index}", input=example["input"],
            expected=example["expected"], meta="{}", index=index, tool_calls="[]", score_details="{}",
        ))
    record_audit(session, request, action="template.instantiate", target_type="agent", target_id=agent.id,
                 team_id=agent.team_id, detail={"template_id": template_id, "agent_version_id": version.id})
    await session.commit()
    gates = await create_automatic_gates(session, version, triggered_by=get_principal(request).user_id)
    for gate in gates:
        submit_gate(gate.id, background)
    await session.refresh(agent)
    return {"agent": _serialize_agent(agent), "agent_version_id": version.id,
            "example_case_ids": case_ids, "template_id": template_id}


__all__ = ["router"]

"""Version-locked deployment management and independent-token delivery."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import AgentLoop
from app.agent.versions import agent_from_version
from app.api.auth import get_principal
from app.audit import record_audit
from app.database import get_session
from app.models import AgentVersion, Deployment, GateBypass, KnowledgeBinding
from app.rbac import accessible_team_ids, authorize, scoped_agent_ids
from app.secrets import redact_config

router = APIRouter(prefix="/deployments", tags=["deployments"])
public_router = APIRouter(tags=["delivery"])


class DeploymentCreate(BaseModel):
    agent_version_id: str
    name: str = Field(min_length=1, max_length=200)


class DeploymentVersion(BaseModel):
    agent_version_id: str


class ChatInput(BaseModel):
    input: str = Field(min_length=1)
    session_id: str | None = None


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except (TypeError, ValueError):
        return fallback


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _out(row: Deployment, raw_token: str | None = None) -> dict[str, Any]:
    result = {"id": row.id, "agent_id": row.agent_id, "team_id": row.team_id,
              "agent_version_id": row.agent_version_id, "name": row.name, "status": row.status,
              "token_prefix": row.token_prefix, "token_revoked": row.token_revoked,
              "runtime_spec": redact_config(_json(row.runtime_spec, {})),
              "version_history": _json(row.version_history, []), "published_by": row.published_by,
              "created_at": row.created_at, "updated_at": row.updated_at}
    if raw_token:
        result.update({"token": raw_token, "api_url": f"/api/deliver/{raw_token}/chat",
                       "page_url": f"/deliver/{raw_token}", "embed_url": f"/embed/{raw_token}"})
    return result


async def _spec(session: AsyncSession, version: AgentVersion) -> dict[str, Any]:
    kb_ids = list((await session.execute(
        select(KnowledgeBinding.kb_id).where(KnowledgeBinding.agent_id == version.agent_id)
    )).scalars().all())
    return {"agent_version_id": version.id, "agent_snapshot": _json(version.snapshot, {}),
            "knowledge_base_ids": kb_ids}


async def _write_access(request: Request, session: AsyncSession, team_id: str | None) -> None:
    if get_principal(request).role != "admin":
        if not team_id:
            raise HTTPException(403, "team-admin approval required")
        await authorize(request, session, "approval:manage", team_id)


async def _read_access(request: Request, session: AsyncSession, row: Deployment | None) -> Deployment:
    if row is None:
        raise HTTPException(404, "deployment not found")
    if get_principal(request).role != "admin":
        teams = await accessible_team_ids(session, request) or set()
        if row.team_id:
            if row.team_id not in teams:
                raise HTTPException(404, "deployment not found")
        elif row.published_by != get_principal(request).user_id:
            raise HTTPException(404, "deployment not found")
    return row


def _check_gate(version: AgentVersion, bypasses: list[GateBypass]) -> None:
    if version.gate_status == "passed":
        return
    if version.gate_status == "bypassed" and any(row.active for row in bypasses):
        return
    raise HTTPException(409, "deployment requires gate_status=passed or an active bypass")


@router.post("", status_code=201)
async def publish(payload: DeploymentCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    version = await session.get(AgentVersion, payload.agent_version_id)
    if version is None:
        raise HTTPException(404, "agent version not found")
    if get_principal(request).role != "admin" and version.agent_id not in (await scoped_agent_ids(session, request) or set()):
        raise HTTPException(404, "agent version not found")
    await _write_access(request, session, version.team_id)
    bypasses = (await session.execute(select(GateBypass).where(GateBypass.agent_version_id == version.id))).scalars().all()
    _check_gate(version, list(bypasses))
    raw_token = secrets.token_urlsafe(32)
    row = Deployment(
        agent_id=version.agent_id, team_id=version.team_id, agent_version_id=version.id,
        name=payload.name.strip(), token_hash=_hash(raw_token), token_prefix=raw_token[:8],
        runtime_spec=json.dumps(await _spec(session, version), ensure_ascii=False),
        version_history=json.dumps([version.id]), published_by=get_principal(request).user_id,
    )
    session.add(row)
    await session.flush()
    record_audit(session, request, action="deployment.publish", target_type="deployment", target_id=row.id,
                 team_id=row.team_id, detail={"agent_version_id": version.id})
    await session.commit()
    return _out(row, raw_token)


@router.get("")
async def list_deployments(request: Request, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(Deployment).order_by(Deployment.created_at.desc())
    if get_principal(request).role != "admin":
        teams = await accessible_team_ids(session, request) or set()
        stmt = stmt.where(Deployment.team_id.in_(teams)) if teams else stmt.where(
            Deployment.published_by == get_principal(request).user_id
        )
    return [_out(row) for row in (await session.execute(stmt)).scalars().all()]


@router.get("/{deployment_id}")
async def get_deployment(deployment_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return _out(await _read_access(request, session, await session.get(Deployment, deployment_id)))


async def _manage(deployment_id: str, request: Request, session: AsyncSession) -> Deployment:
    row = await _read_access(request, session, await session.get(Deployment, deployment_id))
    await _write_access(request, session, row.team_id)
    return row


async def _audit_change(session: AsyncSession, request: Request, row: Deployment, action: str) -> None:
    row.updated_at = datetime.utcnow()
    record_audit(session, request, action=action, target_type="deployment", target_id=row.id,
                 team_id=row.team_id, detail={"agent_version_id": row.agent_version_id})
    await session.commit()


@router.post("/{deployment_id}/publish")
async def publish_new_version(deployment_id: str, payload: DeploymentVersion, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _manage(deployment_id, request, session)
    version = await session.get(AgentVersion, payload.agent_version_id)
    if version is None or version.agent_id != row.agent_id:
        raise HTTPException(404, "agent version not found")
    bypasses = (await session.execute(select(GateBypass).where(GateBypass.agent_version_id == version.id))).scalars().all()
    _check_gate(version, list(bypasses))
    history = _json(row.version_history, [])
    history.append(version.id)
    row.agent_version_id = version.id
    row.version_history = json.dumps(history)
    row.runtime_spec = json.dumps(await _spec(session, version), ensure_ascii=False)
    await _audit_change(session, request, row, "deployment.publish")
    return _out(row)


@router.post("/{deployment_id}/rollback")
async def rollback(deployment_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _manage(deployment_id, request, session)
    history = _json(row.version_history, [])
    if len(history) < 2:
        raise HTTPException(409, "no previous published version")
    history.pop()
    version = await session.get(AgentVersion, history[-1])
    if version is None:
        raise HTTPException(409, "previous version no longer exists")
    row.agent_version_id = version.id
    row.version_history = json.dumps(history)
    row.runtime_spec = json.dumps(await _spec(session, version), ensure_ascii=False)
    await _audit_change(session, request, row, "deployment.rollback")
    return _out(row)


@router.post("/{deployment_id}/stop")
async def stop(deployment_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _manage(deployment_id, request, session)
    row.status = "stopped"
    await _audit_change(session, request, row, "deployment.stop")
    return _out(row)


@router.post("/{deployment_id}/start")
async def start(deployment_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _manage(deployment_id, request, session)
    if row.token_revoked:
        raise HTTPException(409, "rotate the revoked token before starting")
    row.status = "active"
    await _audit_change(session, request, row, "deployment.start")
    return _out(row)


@router.post("/{deployment_id}/revoke")
async def revoke(deployment_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _manage(deployment_id, request, session)
    row.token_revoked = True
    row.status = "stopped"
    await _audit_change(session, request, row, "deployment.revoke")
    return _out(row)


@router.post("/{deployment_id}/rotate-token")
async def rotate(deployment_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _manage(deployment_id, request, session)
    raw_token = secrets.token_urlsafe(32)
    row.token_hash = _hash(raw_token)
    row.token_prefix = raw_token[:8]
    row.token_revoked = False
    await _audit_change(session, request, row, "deployment.rotate_token")
    return _out(row, raw_token)


@public_router.post("/api/deliver/{token}/chat")
async def deliver_chat(token: str, payload: ChatInput, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = (await session.execute(select(Deployment).where(Deployment.token_hash == _hash(token)))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "deployment not found")
    if row.token_revoked:
        raise HTTPException(410, "deployment token has been revoked")
    if row.status != "active":
        raise HTTPException(403, "deployment is stopped")
    version = await session.get(AgentVersion, row.agent_version_id)
    if version is None:
        raise HTTPException(409, "published version no longer exists")
    agent = agent_from_version(version)
    spec = _json(row.runtime_spec, {})
    config = _json(agent.config, {})
    config["knowledge_base_ids"] = spec.get("knowledge_base_ids") or []
    agent.config = json.dumps(config, ensure_ascii=False)
    result = await AgentLoop(agent, session).run(payload.input, session_id=payload.session_id)
    if result.status not in {"success", "completed", "ok"}:
        raise HTTPException(500, {"error": "agent execution failed", "trace_id": result.run_id, "detail": result.error})
    return {"output": result.output, "trace_id": result.run_id, "status": result.status,
            "deployment_id": row.id, "agent_version_id": row.agent_version_id,
            "tokens": result.total_tokens}


__all__ = ["router", "public_router"]

"""Organization, team membership, audit, and high-risk approval APIs."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_principal, require_admin
from app.audit import record_audit
from app.database import get_session
from app.models import ApprovalRequest, AuditLog, Membership, Organization, Team, ToolMetadata, User
from app.rbac import HIGH_RISK_ACTIONS, TEAM_ROLES, accessible_team_ids, authorize, team_role

router = APIRouter(tags=["teams"])


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TeamCreate(BaseModel):
    org_id: str
    name: str = Field(min_length=1, max_length=200)


class MemberCreate(BaseModel):
    email: str
    role: str = "developer"

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        if value not in TEAM_ROLES:
            raise ValueError("role must be team-admin, developer, or viewer")
        return value


class RoleChange(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        if value not in TEAM_ROLES:
            raise ValueError("invalid team role")
        return value


class ApprovalCreate(BaseModel):
    team_id: str
    action: str
    target_type: str
    target_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=3, max_length=2000)


class ApprovalResolve(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


def _org_out(row: Organization) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "created_at": row.created_at}


def _team_out(row: Team, role: str | None = None) -> dict[str, Any]:
    return {"id": row.id, "org_id": row.org_id, "name": row.name,
            "created_by": row.created_by, "created_at": row.created_at, "role": role}


def _member_out(row: Membership, email: str | None = None) -> dict[str, Any]:
    return {"id": row.id, "team_id": row.team_id, "user_id": row.user_id,
            "email": email, "role": row.role, "created_at": row.created_at}


def _approval_out(row: ApprovalRequest) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload or "{}")
    except (TypeError, ValueError):
        payload = {}
    return {"id": row.id, "team_id": row.team_id, "requester_id": row.requester_id,
            "action": row.action, "target_type": row.target_type, "target_id": row.target_id,
            "payload": payload, "reason": row.reason, "status": row.status,
            "resolver_id": row.resolver_id, "resolution_reason": row.resolution_reason,
            "created_at": row.created_at, "resolved_at": row.resolved_at}


@router.post("/orgs", status_code=201, dependencies=[Depends(require_admin)])
async def create_org(payload: OrgCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = Organization(name=payload.name.strip())
    session.add(row)
    await session.flush()
    record_audit(session, request, action="org.create", target_type="organization", target_id=row.id)
    await session.commit()
    return _org_out(row)


@router.get("/orgs")
async def list_orgs(request: Request, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    if get_principal(request).role == "admin":
        rows = (await session.execute(select(Organization).order_by(Organization.name))).scalars().all()
    else:
        rows = (await session.execute(
            select(Organization).join(Team, Team.org_id == Organization.id)
            .join(Membership, Membership.team_id == Team.id)
            .where(Membership.user_id == get_principal(request).user_id).distinct()
        )).scalars().all()
    return [_org_out(row) for row in rows]


@router.post("/teams", status_code=201, dependencies=[Depends(require_admin)])
async def create_team(payload: TeamCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if await session.get(Organization, payload.org_id) is None:
        raise HTTPException(404, "organization not found")
    row = Team(org_id=payload.org_id, name=payload.name.strip(), created_by=get_principal(request).user_id)
    session.add(row)
    await session.flush()
    record_audit(session, request, action="team.create", target_type="team", target_id=row.id, team_id=row.id)
    await session.commit()
    return _team_out(row)


@router.get("/teams")
async def list_teams(request: Request, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    principal = get_principal(request)
    if principal.role == "admin":
        rows = (await session.execute(select(Team).order_by(Team.created_at))).scalars().all()
        return [_team_out(row, "super-admin") for row in rows]
    pairs = (await session.execute(
        select(Team, Membership.role).join(Membership, Membership.team_id == Team.id)
        .where(Membership.user_id == principal.user_id).order_by(Team.created_at)
    )).all()
    return [_team_out(team, role) for team, role in pairs]


@router.get("/teams/{team_id}/members")
async def list_members(team_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    if get_principal(request).role != "admin":
        if await team_role(session, get_principal(request).user_id or "", team_id) is None:
            raise HTTPException(404, "team not found")
    pairs = (await session.execute(
        select(Membership, User.email).join(User, User.id == Membership.user_id)
        .where(Membership.team_id == team_id).order_by(Membership.created_at)
    )).all()
    return [_member_out(row, email) for row, email in pairs]


@router.post("/teams/{team_id}/members", status_code=201)
async def add_member(team_id: str, payload: MemberCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if get_principal(request).role != "admin":
        await authorize(request, session, "member:manage", team_id)
    user = (await session.execute(select(User).where(User.email == payload.email.strip().lower()))).scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "registered user with this email not found")
    row = Membership(team_id=team_id, user_id=user.id, role=payload.role)
    session.add(row)
    try:
        await session.flush()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(409, "user is already a team member") from exc
    record_audit(session, request, action="member.add", target_type="membership", target_id=row.id,
                 team_id=team_id, detail={"user_id": user.id, "role": row.role})
    await session.commit()
    return _member_out(row, user.email)


@router.patch("/teams/{team_id}/members/{user_id}")
async def change_member_role(team_id: str, user_id: str, payload: RoleChange, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if get_principal(request).role != "admin":
        await authorize(request, session, "member:manage", team_id)
    row = (await session.execute(select(Membership).where(
        Membership.team_id == team_id, Membership.user_id == user_id
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "membership not found")
    previous = row.role
    row.role = payload.role
    record_audit(session, request, action="member.role", target_type="membership", target_id=row.id,
                 team_id=team_id, detail={"user_id": user_id, "from": previous, "to": row.role})
    await session.commit()
    return _member_out(row)


@router.delete("/teams/{team_id}/members/{user_id}", status_code=204, response_model=None)
async def remove_member(team_id: str, user_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> None:
    if get_principal(request).role != "admin":
        await authorize(request, session, "member:manage", team_id)
    row = (await session.execute(select(Membership).where(
        Membership.team_id == team_id, Membership.user_id == user_id
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "membership not found")
    if row.role == "team-admin":
        admins = await session.scalar(select(func.count(Membership.id)).where(
            Membership.team_id == team_id, Membership.role == "team-admin"
        ))
        if int(admins or 0) <= 1:
            raise HTTPException(409, "cannot remove the last team-admin")
    record_audit(session, request, action="member.remove", target_type="membership", target_id=row.id,
                 team_id=team_id, detail={"user_id": user_id})
    await session.delete(row)
    await session.commit()



@router.get("/audit")
async def list_audit(
    request: Request, team_id: str | None = None, actor_id: str | None = None,
    action: str | None = None, since: datetime | None = None, until: datetime | None = None,
    limit: int = 100, session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if get_principal(request).role != "admin":
        teams = await accessible_team_ids(session, request) or set()
        if team_id:
            await authorize(request, session, "audit:read", team_id)
            stmt = stmt.where(AuditLog.team_id == team_id)
        elif teams:
            roles = [await team_role(session, get_principal(request).user_id or "", item) for item in teams]
            if "team-admin" not in roles:
                raise HTTPException(403, "audit:read required")
            stmt = stmt.where(AuditLog.team_id.in_(teams))
        else:
            raise HTTPException(403, "audit:read required")
    elif team_id:
        stmt = stmt.where(AuditLog.team_id == team_id)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if since:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until:
        stmt = stmt.where(AuditLog.created_at <= until)
    rows = (await session.execute(stmt)).scalars().all()
    return [{"id": row.id, "actor_id": row.actor_id, "team_id": row.team_id,
             "action": row.action, "target_type": row.target_type, "target_id": row.target_id,
             "detail": json.loads(row.detail or "{}"), "ip": row.ip, "created_at": row.created_at}
            for row in rows]


@router.post("/approvals", status_code=201)
async def request_approval(payload: ApprovalCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if payload.action not in HIGH_RISK_ACTIONS:
        raise HTTPException(400, f"action must be one of {sorted(HIGH_RISK_ACTIONS)}")
    principal = get_principal(request)
    if principal.role != "admin":
        role = await team_role(session, principal.user_id or "", payload.team_id)
        if role is None:
            raise HTTPException(404, "team not found")
        if role == "viewer":
            raise HTTPException(403, "viewer cannot request high-risk operations")
    row = ApprovalRequest(
        team_id=payload.team_id, requester_id=principal.user_id or "static-admin",
        action=payload.action, target_type=payload.target_type, target_id=payload.target_id,
        payload=json.dumps(payload.payload, ensure_ascii=False), reason=payload.reason,
    )
    session.add(row)
    await session.flush()
    record_audit(session, request, action="approval.request", target_type="approval", target_id=row.id,
                 team_id=row.team_id, detail={"operation": row.action, "target_id": row.target_id})
    await session.commit()
    return _approval_out(row)


@router.get("/approvals")
async def list_approvals(request: Request, team_id: str | None = None, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())
    principal = get_principal(request)
    if principal.role != "admin":
        teams = await accessible_team_ids(session, request) or set()
        if team_id:
            if team_id not in teams:
                raise HTTPException(404, "team not found")
            stmt = stmt.where(ApprovalRequest.team_id == team_id)
        elif teams:
            stmt = stmt.where(ApprovalRequest.team_id.in_(teams))
        else:
            return []
        if team_id and await team_role(session, principal.user_id or "", team_id) != "team-admin":
            stmt = stmt.where(ApprovalRequest.requester_id == principal.user_id)
    elif team_id:
        stmt = stmt.where(ApprovalRequest.team_id == team_id)
    return [_approval_out(row) for row in (await session.execute(stmt)).scalars().all()]


async def _resolve_approval(approval_id: str, status: str, payload: ApprovalResolve, request: Request, session: AsyncSession) -> dict[str, Any]:
    row = await session.get(ApprovalRequest, approval_id)
    if row is None:
        raise HTTPException(404, "approval request not found")
    if get_principal(request).role != "admin":
        await authorize(request, session, "approval:manage", row.team_id)
    if row.status != "pending":
        raise HTTPException(409, "approval request is already resolved")
    row.status = status
    row.resolver_id = get_principal(request).user_id
    row.resolution_reason = payload.reason
    row.resolved_at = datetime.utcnow()
    if status == "approved" and row.action == "tool.enable":
        data = json.loads(row.payload or "{}")
        name = str(data.get("name") or row.target_id)
        tool = await session.get(ToolMetadata, name)
        if tool is None:
            tool = ToolMetadata(name=name, team_id=row.team_id, enabled=True)
            session.add(tool)
        tool.enabled = True
        tool.team_id = row.team_id
        record_audit(session, request, action="tool.enable", target_type="tool", target_id=name,
                     team_id=row.team_id, detail={"approval_id": row.id})
    record_audit(session, request, action=f"approval.{status}", target_type="approval", target_id=row.id,
                 team_id=row.team_id, detail={"operation": row.action, "reason": payload.reason})
    await session.commit()
    return _approval_out(row)


@router.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str, payload: ApprovalResolve, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return await _resolve_approval(approval_id, "approved", payload, request, session)


@router.post("/approvals/{approval_id}/reject")
async def reject(approval_id: str, payload: ApprovalResolve, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return await _resolve_approval(approval_id, "rejected", payload, request, session)


__all__ = ["router"]

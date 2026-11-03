"""Central team RBAC matrix and mandatory resource-scope helpers."""
from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Agent, EvalCase, Membership

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "super-admin": frozenset({"*"}),
    "team-admin": frozenset({
        "agent:create", "agent:read", "agent:update", "agent:delete",
        "run:execute", "run:pause", "run:cancel", "eval:create", "eval:run", "eval:read",
        "memory:read", "memory:write", "tool:create", "tool:read", "gate:trigger",
        "gate:override", "gate:read", "member:manage", "audit:read", "approval:manage",
    }),
    "developer": frozenset({
        "agent:create", "agent:read", "agent:update", "agent:delete",
        "run:execute", "run:pause", "run:cancel", "eval:create", "eval:run", "eval:read",
        "memory:read", "memory:write", "tool:read", "gate:trigger", "gate:read",
    }),
    "viewer": frozenset({"agent:read", "eval:read", "memory:read", "tool:read", "gate:read"}),
}
TEAM_ROLES = frozenset({"team-admin", "developer", "viewer"})
HIGH_RISK_ACTIONS = frozenset({
    "tool.enable", "gate.override", "resource.transfer", "deployment.publish",
})


def _principal(request: Request):
    from app.api.auth import get_principal
    return get_principal(request)


def _auth_enabled() -> bool:
    from app.api.auth import _auth_required
    return _auth_required()


def _allowed(role: str, permission: str) -> bool:
    permissions = ROLE_PERMISSIONS.get(role, frozenset())
    return "*" in permissions or permission in permissions


async def memberships(session: AsyncSession, user_id: str) -> list[Membership]:
    return list((await session.execute(
        select(Membership).where(Membership.user_id == user_id)
    )).scalars().all())


async def team_role(session: AsyncSession, user_id: str, team_id: str) -> str | None:
    return await session.scalar(
        select(Membership.role).where(Membership.user_id == user_id, Membership.team_id == team_id)
    )


async def accessible_team_ids(session: AsyncSession, request: Request) -> set[str] | None:
    """None means unrestricted super-admin; a set is a mandatory team scope."""
    if not _auth_enabled():
        return None
    principal = _principal(request)
    if principal.role == "admin":
        return None
    if not principal.user_id:
        return set()
    return {row.team_id for row in await memberships(session, principal.user_id)}


async def authorize(
    request: Request, session: AsyncSession, permission: str, team_id: str | None = None,
) -> None:
    if not _auth_enabled() or _principal(request).role == "admin":
        return
    principal = _principal(request)
    if not principal.user_id:
        raise HTTPException(403, "permission denied")
    if team_id is not None:
        role = await team_role(session, principal.user_id, team_id)
        if role is None or not _allowed(role, permission):
            raise HTTPException(403, f"{permission} required for this team")
        return
    rows = await memberships(session, principal.user_id)
    if rows:
        if any(_allowed(row.role, permission) for row in rows):
            return
        raise HTTPException(403, f"{permission} required")
    # Backward-compatible personal workspace before a user joins any team.
    if permission in ROLE_PERMISSIONS["developer"] and permission not in {"agent:create"}:
        return
    if permission == "agent:create":
        return
    raise HTTPException(403, f"{permission} required")


def require_permission(permission: str):
    async def dependency(request: Request, session: AsyncSession = Depends(get_session)) -> None:
        await authorize(request, session, permission)
    return dependency


async def resolve_team_for_create(
    request: Request, session: AsyncSession, requested_team_id: str | None,
) -> str | None:
    requested = requested_team_id or request.headers.get("x-team-id")
    if not _auth_enabled() or _principal(request).role == "admin":
        return requested
    principal = _principal(request)
    if not principal.user_id:
        raise HTTPException(403, "permission denied")
    rows = await memberships(session, principal.user_id)
    if requested:
        await authorize(request, session, "agent:create", requested)
        return requested
    if not rows:
        return None  # legacy personal workspace
    allowed = [row.team_id for row in rows if _allowed(row.role, "agent:create")]
    if len(allowed) == 1:
        return allowed[0]
    if not allowed:
        raise HTTPException(403, "agent:create required")
    raise HTTPException(400, "team_id is required for users in multiple teams")


def agent_scope_condition(team_ids: set[str] | None, user_id: str | None) -> Any:
    if team_ids is None:
        return None
    conditions = []
    if team_ids:
        conditions.append(Agent.team_id.in_(team_ids))
    if user_id:
        conditions.append(and_(Agent.team_id.is_(None), Agent.owner_id == user_id))
    return or_(*conditions) if conditions else Agent.id.is_(None)


def permission_for_agent_request(request: Request) -> str:
    method = request.method.upper()
    path = request.url.path
    if method == "GET":
        if "/memory" in path:
            return "memory:read"
        if "/eval" in path:
            return "eval:read"
        if "/gates" in path or "/versions" in path:
            return "gate:read"
        return "agent:read"
    if "/memory" in path:
        return "memory:write"
    if "/gates" in path or "/bypass" in path:
        return "gate:override" if "/bypass" in path else "gate:trigger"
    if "/eval" in path or "/evaluate" in path:
        return "eval:run" if method == "POST" else "eval:create"
    if "/runs" in path:
        if path.endswith("/pause") or path.endswith("/resume"):
            return "run:pause"
        if path.endswith("/cancel"):
            return "run:cancel"
        return "run:execute"
    return {"POST": "agent:update", "PATCH": "agent:update", "PUT": "agent:update",
            "DELETE": "agent:delete"}.get(method, "agent:read")


async def authorize_agent(
    request: Request, session: AsyncSession, agent: Agent | None,
) -> None:
    if agent is None:
        raise HTTPException(404, "agent not found")
    if not _auth_enabled() or _principal(request).role == "admin":
        return
    principal = _principal(request)
    permission = permission_for_agent_request(request)
    if agent.team_id:
        role = await team_role(session, principal.user_id or "", agent.team_id)
        if role is None:
            raise HTTPException(404, "agent not found")
        if not _allowed(role, permission):
            raise HTTPException(403, f"{permission} required")
        # Developers may delete their own Agents, not teammates' Agents.
        if (permission == "agent:delete" and role == "developer"
                and agent.owner_id != principal.user_id):
            raise HTTPException(404, "agent not found")
        return
    if agent.owner_id != principal.user_id:
        raise HTTPException(404, "agent not found")
    # Existing personal rows retain the stage-4 owner boundary.
    if permission not in ROLE_PERMISSIONS["developer"]:
        raise HTTPException(403, f"{permission} required")


def eval_case_scope(agent: Agent) -> Any:
    """Cases referenced by a run/suite must share the Agent's tenant."""
    if agent.team_id:
        return EvalCase.team_id == agent.team_id
    return and_(
        EvalCase.team_id.is_(None),
        (EvalCase.owner_id == agent.owner_id) | EvalCase.owner_id.is_(None),
    )


async def scoped_agent_ids(session: AsyncSession, request: Request) -> set[str] | None:
    teams = await accessible_team_ids(session, request)
    if teams is None:
        return None
    principal = _principal(request)
    condition = agent_scope_condition(teams, principal.user_id)
    return set((await session.execute(select(Agent.id).where(condition))).scalars().all())


__all__ = [
    "ROLE_PERMISSIONS", "TEAM_ROLES", "HIGH_RISK_ACTIONS", "memberships", "team_role",
    "accessible_team_ids", "authorize", "require_permission", "resolve_team_for_create",
    "agent_scope_condition", "permission_for_agent_request", "authorize_agent", "scoped_agent_ids",
    "eval_case_scope",
]

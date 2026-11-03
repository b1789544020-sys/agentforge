"""Static-token/JWT authentication and centralized ownership checks."""
from __future__ import annotations

import base64
import secrets

from fastapi import Depends, HTTPException, Request, WebSocket, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.models import Agent
from app.security import Principal, SYSTEM_PRINCIPAL, decode_access_token


def _auth_required() -> bool:
    settings = get_settings()
    return settings.auth_enabled or bool(settings.api_token)


def _bearer_token(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _static_matches(candidate: str | None) -> bool:
    expected = get_settings().api_token
    return bool(expected and candidate) and secrets.compare_digest(
        candidate.encode("utf-8"), expected.encode("utf-8")
    )


def _authenticate(candidate: str | None) -> Principal | None:
    if _static_matches(candidate):
        return SYSTEM_PRINCIPAL
    return decode_access_token(candidate) if candidate else None


async def require_api_token(request: Request) -> None:
    """Populate a shared Principal for legacy tokens and user JWTs."""
    if not _auth_required():
        request.state.principal = SYSTEM_PRINCIPAL
        return
    candidate = request.headers.get("x-api-key") or _bearer_token(
        request.headers.get("authorization")
    )
    principal = _authenticate(candidate)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.principal = principal


def get_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None and not _auth_required():
        return SYSTEM_PRINCIPAL
    if principal is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return principal


async def require_agent_access(
    request: Request,
    agent_id: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Enforce Agent ownership before any nested resource can be accessed."""
    if not _auth_required():
        return
    principal = get_principal(request)
    if principal.role == "admin" or agent_id is None:
        return
    agent = await session.get(Agent, agent_id)
    from app.rbac import authorize_agent
    await authorize_agent(request, session, agent)


async def require_admin(request: Request) -> None:
    if _auth_required() and get_principal(request).role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")


def _websocket_subprotocol_token(websocket: WebSocket) -> str | None:
    offered = websocket.headers.get("sec-websocket-protocol", "")
    for item in offered.split(","):
        value = item.strip()
        if not value.startswith("bearer."):
            continue
        encoded = value[len("bearer.") :]
        try:
            return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return None


def get_websocket_principal(websocket: WebSocket) -> Principal | None:
    if not _auth_required():
        return SYSTEM_PRINCIPAL
    candidate = (
        websocket.headers.get("x-api-key")
        or _bearer_token(websocket.headers.get("authorization"))
        or _websocket_subprotocol_token(websocket)
    )
    return _authenticate(candidate)


async def require_websocket_token(websocket: WebSocket) -> bool:
    if get_websocket_principal(websocket) is not None:
        return True
    await websocket.close(code=4401, reason="invalid or missing API token")
    return False

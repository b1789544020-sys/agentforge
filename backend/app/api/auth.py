"""Small static-token authentication boundary for self-hosted deployments."""
from __future__ import annotations

import base64
import secrets

from fastapi import Header, HTTPException, WebSocket, status

from app.config import get_settings


def _matches(candidate: str | None) -> bool:
    expected = get_settings().api_token
    if not expected:
        return True
    if not candidate:
        return False
    return secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _bearer_token(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def require_api_token(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    """Accept either ``Authorization: Bearer`` or ``X-API-Key``.

    An unset token deliberately preserves the local development mode.  Once
    configured, authentication is mandatory and health endpoints stay public.
    """
    if _matches(x_api_key or _bearer_token(authorization)):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing API token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _websocket_subprotocol_token(websocket: WebSocket) -> str | None:
    """Read a base64url token from the WebSocket protocol header.

    Browsers cannot set arbitrary WebSocket headers.  A subprotocol carries
    the token without placing the long-lived credential in URLs or logs.
    """
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


async def require_websocket_token(websocket: WebSocket) -> bool:
    candidate = (
        websocket.headers.get("x-api-key")
        or _bearer_token(websocket.headers.get("authorization"))
        or _websocket_subprotocol_token(websocket)
    )
    if _matches(candidate):
        return True
    await websocket.close(code=4401, reason="invalid or missing API token")
    return False

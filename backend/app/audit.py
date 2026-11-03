"""Internal append-only audit writer; no public mutation endpoint exists."""
from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


def record_audit(
    session: AsyncSession, request: Request | None, *, action: str,
    target_type: str, target_id: str, team_id: str | None = None,
    detail: dict[str, Any] | None = None, actor_id: str | None = None,
) -> AuditLog:
    if request is not None:
        from app.api.auth import get_principal
        actor_id = get_principal(request).user_id
        ip = request.client.host if request.client else None
    else:
        ip = None
    row = AuditLog(
        actor_id=actor_id, team_id=team_id, action=action,
        target_type=target_type, target_id=target_id,
        detail=json.dumps(detail or {}, ensure_ascii=False), ip=ip,
    )
    session.add(row)
    return row


__all__ = ["record_audit"]

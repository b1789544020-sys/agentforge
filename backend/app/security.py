"""Password, JWT, and principal helpers shared by HTTP and WebSocket auth."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.config import get_settings


@dataclass(frozen=True)
class Principal:
    user_id: str | None
    role: str
    is_static: bool = False


SYSTEM_PRINCIPAL = Principal(user_id=None, role="admin", is_static=True)


def hash_password(password: str) -> str:
    _validate_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False


def _validate_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("password must contain at least 8 characters")
    if len(password.encode("utf-8")) > 72:
        raise ValueError("password must not exceed 72 UTF-8 bytes (bcrypt limit)")


def create_access_token(user_id: str, role: str) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Principal | None:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=[get_settings().jwt_algorithm],
        )
        user_id = payload.get("sub")
        if payload.get("type") != "access" or not isinstance(user_id, str) or not user_id:
            return None
        role = payload.get("role") if payload.get("role") in {"admin", "user"} else "user"
        return Principal(user_id=user_id, role=role)
    except (JWTError, ValueError, TypeError):
        return None


__all__ = [
    "Principal", "SYSTEM_PRINCIPAL", "hash_password", "verify_password",
    "create_access_token", "decode_access_token",
]

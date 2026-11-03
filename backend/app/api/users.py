"""Registration, login, current-user, and role administration routes."""
from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_principal, require_admin, require_api_token
from app.database import get_session
from app.models import User
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(tags=["auth"])
_attempts: dict[str, list[float]] = {}


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("valid email required")
        return value


class RoleUpdate(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        if value not in {"admin", "user"}:
            raise ValueError("role must be admin or user")
        return value


def _user_payload(user: User) -> dict:
    return {
        "id": user.id, "email": user.email, "role": user.role,
        "is_active": user.is_active, "created_at": user.created_at,
    }


def _token_payload(user: User) -> dict:
    return {
        "access_token": create_access_token(user.id, user.role),
        "token_type": "bearer",
        "user": _user_payload(user),
    }


def _rate_limit(request: Request) -> None:
    now = time.monotonic()
    key = f"{request.client.host if request.client else 'unknown'}:{request.url.path}"
    recent = [stamp for stamp in _attempts.get(key, []) if now - stamp < 60]
    if len(recent) >= 10:
        raise HTTPException(status_code=429, detail="too many attempts; retry later")
    recent.append(now)
    _attempts[key] = recent


@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: Credentials, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    _rate_limit(request)
    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    is_first = (await session.scalar(select(func.count(User.id))) or 0) == 0
    user = User(
        id=str(uuid.uuid4()), email=payload.email, password_hash=password_hash,
        role="admin" if is_first else "user",
    )
    session.add(user)
    try:
        await session.commit()
    except Exception as exc:
        await session.rollback()
        if "unique" in str(exc).lower() or "integrity" in type(exc).__name__.lower():
            raise HTTPException(status_code=409, detail="email already registered") from exc
        raise
    await session.refresh(user)
    return _token_payload(user)


@router.post("/auth/login")
async def login(
    payload: Credentials, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    _rate_limit(request)
    user = (await session.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    return _token_payload(user)


@router.get("/auth/me", dependencies=[Depends(require_api_token)])
async def me(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    principal = get_principal(request)
    if principal.is_static:
        return {"id": None, "email": None, "role": principal.role, "is_static": True}
    user = await session.get(User, principal.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user is inactive or no longer exists")
    return _user_payload(user)


@router.get("/admin/users", dependencies=[Depends(require_api_token), Depends(require_admin)])
async def list_users(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
    return [_user_payload(user) for user in rows]


@router.patch("/admin/users/{user_id}/role", dependencies=[Depends(require_api_token), Depends(require_admin)])
async def update_role(
    user_id: str, payload: RoleUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user.role = payload.role
    await session.commit()
    return _user_payload(user)

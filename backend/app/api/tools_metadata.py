"""CRUD endpoints for tool-metadata plugin overrides (0.1.3 #3).

The runtime metadata of every built-in tool lives in
:class:`app.models.ToolMetadata`.  This module exposes:

* ``GET    /api/tools``         -- merged view (registry + DB overrides);
                                   *replaces* the older in-place listing in
                                   ``app.api.agents.list_tools``.
* ``GET    /api/tools/{name}``  -- single merged view (404 if the tool is
                                   not registered).
* ``PUT    /api/tools/{name}/metadata``  -- upsert an override (sparse
                                   PATCH semantics: missing fields are kept
                                   from the existing row or the registry
                                   default).
* ``DELETE /api/tools/{name}/metadata``  -- drop the override row; the
                                   registry defaults take over again.

The merge is centralised in :func:`merge_metadata` so the agent loop
and the test-suite can use the same code path.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import ToolRegistry
from app.database import get_session
from app.models import ToolMetadata

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools", tags=["tools"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ToolMetadataUpdate(BaseModel):
    """Sparse update payload.

    All fields are optional; missing fields keep the value from the
    existing row (or the registry default if no row exists yet).
    ``enabled`` is the only field that defaults to ``True`` on a
    brand-new row so a PUT that only sets ``description`` does not
    accidentally disable a tool.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = None
    description: Optional[str] = None
    risk: Optional[str] = Field(default=None, max_length=20)
    scope: Optional[str] = Field(default=None, max_length=50)
    category: Optional[str] = Field(default=None, max_length=50)
    parameters_schema: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None


class ToolMetadataOut(BaseModel):
    """Full metadata for a single tool, including the source-of-truth
    fields from the registry and the DB override row.

    The ``source`` field is either ``"registry"`` (no row) or
    ``"override"`` (row present) so the front-end can show a small
    "edited" pill when relevant.
    """

    name: str
    display_name: Optional[str] = None
    description: str
    risk: str
    scope: str
    category: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    source: str = "registry"
    updated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Merge logic (pure)
# ---------------------------------------------------------------------------


def merge_metadata(
    spec_dict: dict[str, Any], row: Optional[ToolMetadata]
) -> dict[str, Any]:
    """Return a merged metadata dict for the ``/api/tools`` payload.

    The registry always wins for ``name`` (it's the primary key on
    both sides).  Every other field is taken from ``row`` when the
    row sets it explicitly (i.e. the column is not ``None``) and from
    ``spec_dict`` otherwise.  The ``source`` field reports which
    side the metadata came from.
    """
    out: dict[str, Any] = dict(spec_dict)
    if row is None:
        out["source"] = "registry"
        out.setdefault("enabled", True)
        out.setdefault("category", None)
        return out

    out["source"] = "override"
    if row.display_name is not None:
        out["display_name"] = row.display_name
    if row.description is not None:
        out["description"] = row.description
    if row.risk is not None:
        out["risk"] = row.risk
    if row.scope is not None:
        out["scope"] = row.scope
    if row.category is not None:
        out["category"] = row.category
    if row.parameters_schema:
        try:
            out["parameters"] = json.loads(row.parameters_schema)
        except (TypeError, ValueError):
            # Malformed blob -- fall back to registry schema so the
            # front-end still sees something sensible.
            pass
    out["enabled"] = bool(row.enabled)
    out["updated_at"] = row.updated_at.isoformat() if row.updated_at else None
    return out


def _spec_to_dict(spec) -> dict[str, Any]:
    """Normalise a :class:`ToolSpec` to the canonical wire shape."""
    params = getattr(spec, "parameters", None) or getattr(spec, "schema", None) or {}
    return {
        "name": spec.name,
        "display_name": None,
        "description": spec.description or "",
        "risk": getattr(spec, "risk", "safe"),
        "scope": getattr(spec, "scope", "general"),
        "category": None,
        "parameters": params,
        "enabled": True,
    }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _load_overrides(
    session: AsyncSession, names: list[str]
) -> dict[str, ToolMetadata]:
    """Return a ``{name: row}`` mapping for the given tool names."""
    if not names:
        return {}
    rows = (
        await session.execute(
            select(ToolMetadata).where(ToolMetadata.name.in_(names))
        )
    ).scalars().all()
    return {r.name: r for r in rows}

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ToolMetadataOut])
async def list_tools(
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """List every registered tool, merged with its DB override row.

    Tools whose row has ``enabled=False`` are still returned (the
    front-end can show them greyed-out in the editor) but flagged via
    the ``enabled`` field so the agent loop excludes them.
    """
    specs = ToolRegistry.list_all()
    overrides = await _load_overrides(session, [s.name for s in specs])
    out: list[dict[str, Any]] = []
    for spec in specs:
        merged = merge_metadata(_spec_to_dict(spec), overrides.get(spec.name))
        out.append(merged)
    return out


@router.get("/{name}", response_model=ToolMetadataOut)
async def get_tool(
    name: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Return the merged metadata for a single tool."""
    spec = ToolRegistry.spec(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"tool {name!r} not registered")
    row = await session.get(ToolMetadata, name)
    return merge_metadata(_spec_to_dict(spec), row)


@router.put("/{name}/metadata", response_model=ToolMetadataOut)
async def upsert_metadata(
    name: str,
    payload: ToolMetadataUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Create or update the metadata override for a tool.

    Sparse PATCH semantics: only fields explicitly set in ``payload``
    are written.  ``enabled`` defaults to ``True`` on a brand-new
    row to match the front-end's "edit one field at a time" workflow.
    """
    spec = ToolRegistry.spec(name)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"tool {name!r} is not registered; cannot attach metadata. "
                "Register the tool first (e.g. via @tool()) then re-PUT."
            ),
        )

    row = await session.get(ToolMetadata, name)
    if row is None:
        row = ToolMetadata(name=name, enabled=True)
        session.add(row)

    if payload.display_name is not None:
        row.display_name = payload.display_name
    if payload.description is not None:
        row.description = payload.description
    if payload.risk is not None:
        row.risk = payload.risk
    if payload.scope is not None:
        row.scope = payload.scope
    if payload.category is not None:
        row.category = payload.category
    if payload.parameters_schema is not None:
        row.parameters_schema = json.dumps(
            payload.parameters_schema, ensure_ascii=False
        )
    if payload.enabled is not None:
        row.enabled = payload.enabled

    await session.commit()
    await session.refresh(row)
    logger.info("tool metadata upsert name=%s enabled=%s", name, row.enabled)
    return merge_metadata(_spec_to_dict(spec), row)


@router.delete("/{name}/metadata", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metadata(
    name: str, session: AsyncSession = Depends(get_session)
) -> None:
    """Drop the override row for a tool.  Registry defaults take over."""
    row = await session.get(ToolMetadata, name)
    if row is None:
        # Idempotent: deleting a non-existent override is a no-op so
        # the front-end can safely fire-and-forget on a "reset" button.
        return
    await session.delete(row)
    await session.commit()


# ---------------------------------------------------------------------------
# Loop-side helper (imported by app.agent.loop)
# ---------------------------------------------------------------------------


async def get_enabled_tool_names(
    session: AsyncSession,
    names=None,
):
    """Return the subset of ``names`` whose metadata row is enabled.

    Tools that have no row at all default to ``enabled=True``, which
    matches the registry's source-of-truth behaviour.  Only rows that
    exist *and* have ``enabled=False`` cause a tool to be hidden.
    """
    from typing import Optional, Set

    all_registry_names = {s.name for s in ToolRegistry.list_all()}
    candidates = list(all_registry_names) if names is None else list(names)
    overrides = await _load_overrides(session, candidates)
    out: Set[str] = set()
    for n in candidates:
        if n not in all_registry_names:
            continue
        row = overrides.get(n)
        if row is None or row.enabled:
            out.add(n)
    return out


__all__ = [
    "router",
    "merge_metadata",
    "get_enabled_tool_names",
]

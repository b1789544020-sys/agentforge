"""CRUD endpoints for :class:`Agent` records."""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.exporter import (
    agents_from_payload,
    build_payload,
    detect_format,
    dump as dump_payload,
    load as load_payload,
)
from app.agent.tools import ToolRegistry
from app.api.schemas import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    ExportRequest,
    MemoryIn,
    MemoryOut,
    ToolOut,
)
from app.database import get_session
from app.models import Agent, Memory

router = APIRouter(prefix="/agents", tags=["agents"])


def _serialize_agent(agent: Agent) -> dict[str, Any]:
    tools: list[str] = []
    if agent.tools:
        try:
            tools = json.loads(agent.tools)
        except (TypeError, ValueError):
            tools = []
    config: dict[str, Any] = {}
    if agent.config:
        try:
            config = json.loads(agent.config)
        except (TypeError, ValueError):
            config = {}
    return {
        "id": agent.id,
        "name": agent.name,
        "description": agent.description,
        "system_prompt": agent.system_prompt,
        "model": agent.model,
        "provider": agent.provider,
        "temperature": agent.temperature,
        "max_steps": agent.max_steps,
        "tools": tools,
        "config": config,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


@router.get("", response_model=list[AgentOut])
async def list_agents(
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (await session.execute(select(Agent).order_by(Agent.created_at.desc()))).scalars().all()
    return [_serialize_agent(a) for a in rows]


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    agent = Agent(
        id=str(uuid.uuid4()),
        name=payload.name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        model=payload.model,
        provider=payload.provider,
        temperature=payload.temperature,
        max_steps=payload.max_steps,
        tools=json.dumps(payload.tools, ensure_ascii=False),
        config=json.dumps(payload.config, ensure_ascii=False),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return _serialize_agent(agent)


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    return _serialize_agent(agent)


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    data = payload.model_dump(exclude_unset=True)
    if "tools" in data and data["tools"] is not None:
        agent.tools = json.dumps(data["tools"], ensure_ascii=False)
        data.pop("tools")
    if "config" in data and data["config"] is not None:
        agent.config = json.dumps(data["config"], ensure_ascii=False)
        data.pop("config")
    for k, v in data.items():
        setattr(agent, k, v)
    agent.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(agent)
    return _serialize_agent(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    await session.delete(agent)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Import / Export (file-based, 0.1.2 #7)
# ---------------------------------------------------------------------------
_MEDIA_TYPES = {
    "json": "application/json; charset=utf-8",
    "yaml": "application/x-yaml; charset=utf-8",
}


def _resolve_export_format(fmt: str | None) -> str:
    fmt = (fmt or "json").lower()
    if fmt not in _MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported format {fmt!r}; expected 'json' or 'yaml'",
        )
    return fmt


@router.post("/export", response_class=Response)
async def export_agents(
    payload: ExportRequest | None = None,
    fmt: str = Query(default="json"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Export agents to a downloadable file (JSON or YAML).

    Request body (optional)::

        {"agent_ids": ["id1", "id2", ...]}     # specific agents
        {}                                       # all agents

    - When ``agent_ids`` is omitted, null, or empty → every agent
      in the database is exported.
    - ``fmt=json|yaml`` → file format (default ``json``).
    """
    fmt = _resolve_export_format(fmt)
    agent_ids = payload.agent_ids if payload else None
    if agent_ids is None:
        rows = (
            await session.execute(select(Agent).order_by(Agent.created_at.desc()))
        ).scalars().all()
    else:
        if not agent_ids:
            raise HTTPException(
                status_code=400, detail="agent_ids must be non-empty if provided"
            )
        rows = (
            await session.execute(select(Agent).where(Agent.id.in_(agent_ids)))
        ).scalars().all()
        found = {a.id for a in rows}
        missing = [aid for aid in agent_ids if aid not in found]
        if missing:
            raise HTTPException(
                status_code=404,
                detail={"error": "agents not found", "missing_ids": missing},
            )

    payload_out = build_payload(rows)
    text = dump_payload(payload_out, fmt)
    filename = (
        f"agentforge-agents-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.{fmt}"
    )
    return Response(
        content=text,
        media_type=_MEDIA_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", response_model=dict)
async def import_agents(
    file: UploadFile = File(...),
    on_conflict: str = Form("skip"),
    fmt: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Import agents from a previously-exported file.

    ``on_conflict`` controls what happens when an agent with the same
    ``name`` already exists:

    - ``skip`` (default) → leave the existing record untouched
    - ``overwrite`` → update its fields in place
    - ``error`` → abort the whole batch with HTTP 409
    """
    if on_conflict not in {"skip", "overwrite", "error"}:
        raise HTTPException(
            status_code=400,
            detail=f"on_conflict must be one of skip/overwrite/error, got {on_conflict!r}",
        )

    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"file is not valid utf-8: {e}")

    detected = fmt.lower() if fmt else detect_format(file.filename, text)
    if detected not in _MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"could not detect format; pass ?fmt=json|yaml (got {detected!r})",
        )

    try:
        payload = load_payload(text, detected)
        rows = agents_from_payload(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    for row in rows:
        name = row["name"]
        existing = (
            await session.execute(select(Agent).where(Agent.name == name))
        ).scalars().first()
        if existing is not None:
            if on_conflict == "skip":
                skipped.append(name)
                continue
            if on_conflict == "error":
                raise HTTPException(
                    status_code=409,
                    detail={"error": "agent name already exists", "name": name},
                )
            # overwrite
            existing.description = row.get("description")
            existing.system_prompt = row.get("system_prompt")
            existing.model = row.get("model", existing.model)
            existing.provider = row.get("provider", existing.provider)
            existing.temperature = row.get("temperature", existing.temperature)
            existing.max_steps = row.get("max_steps", existing.max_steps)
            existing.tools = json.dumps(row.get("tools") or [], ensure_ascii=False)
            existing.config = json.dumps(row.get("config") or {}, ensure_ascii=False)
            existing.updated_at = datetime.utcnow()
            updated.append(name)
            continue

        agent = Agent(
            id=str(uuid.uuid4()),
            name=name,
            description=row.get("description"),
            system_prompt=row.get("system_prompt"),
            model=row.get("model") or "gpt-4o-mini",
            provider=row.get("provider") or "openai",
            temperature=(
                row.get("temperature")
                if row.get("temperature") is not None
                else 0.7
            ),
            max_steps=row.get("max_steps") or 20,
            tools=json.dumps(row.get("tools") or [], ensure_ascii=False),
            config=json.dumps(row.get("config") or {}, ensure_ascii=False),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(agent)
        created.append(name)

    await session.commit()
    return {
        "ok": True,
        "format": detected,
        "on_conflict": on_conflict,
        "imported": len(created),
        "updated": len(updated),
        "skipped": len(skipped),
        "created": created,
        "updated_list": updated,
        "skipped_list": skipped,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# /tools router -- moved to :mod:`app.api.tools_metadata` in 0.1.3.
# The richer endpoint (with DB-merged description / risk / scope /
# category / enabled flag) lives at the same ``/tools`` path on
# :data:`app.api.tools_metadata.router`.  This module is kept as a
# thin placeholder so legacy imports of ``tools_router`` still resolve;
# the prefix is intentionally empty so the new router owns the URL
# surface.  The :func:`list_tools` helper is retained as a library
# function for code that still needs the registry-only view (e.g. a
# future /health-style "what tools are compiled in" probe).
# ---------------------------------------------------------------------------
from app.agent.tools import ToolRegistry  # noqa: E402
from app.api.schemas import ToolOut  # noqa: E402

# NOTE: prefix is empty on purpose -- the canonical ``/tools`` route
# is now served by ``app.api.tools_metadata.router`` (registered in
# ``app.api.__init__`` after this one so it wins on FastAPI's first
# match).  This stub exposes the original library helper for tests.
tools_router = APIRouter(tags=["tools"])


def list_tools() -> list[dict[str, Any]]:
    """Library helper: registry-only tool listing.

    Returns the historical wire shape (name / description / parameters
    / dangerous flag).  The HTTP endpoint at ``GET /tools`` is now
    served by :mod:`app.api.tools_metadata` which merges the DB
    override rows.  Tests that want the legacy view can import this
    function directly.
    """
    out: list[dict[str, Any]] = []
    for t in ToolRegistry.list_all():
        params = getattr(t, "parameters", None) or getattr(t, "schema", None) or {}
        out.append(
            ToolOut(
                name=t.name,
                description=t.description or "",
                parameters=params,
                dangerous=getattr(t, "dangerous", False) or t.risk != "safe",
            ).model_dump()
        )
    return out


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
memory_router = APIRouter(prefix="/agents/{agent_id}/memory", tags=["memory"])


@memory_router.get("", response_model=list[MemoryOut])
async def list_memory(
    agent_id: str, session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Memory).where(Memory.agent_id == agent_id).order_by(Memory.created_at.desc())
        )
    ).scalars().all()
    return [
        {
            "id": m.id,
            "agent_id": m.agent_id,
            "content": m.content,
            "kind": m.kind,
            "importance": m.importance,
            "created_at": m.created_at,
            "meta": json.loads(m.meta) if m.meta else {},
        }
        for m in rows
    ]


@memory_router.post("", response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
async def add_memory(
    agent_id: str,
    payload: MemoryIn,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    mem = Memory(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        kind=payload.kind,
        content=payload.content,
        importance=payload.importance,
        meta=json.dumps(payload.metadata, ensure_ascii=False),
        created_at=datetime.utcnow(),
    )
    session.add(mem)
    await session.commit()
    await session.refresh(mem)
    return {
        "id": mem.id,
        "agent_id": mem.agent_id,
        "content": mem.content,
        "kind": mem.kind,
        "importance": mem.importance,
        "created_at": mem.created_at,
        "meta": json.loads(mem.meta) if mem.meta else {},
    }


@memory_router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    agent_id: str,
    memory_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    mem = await session.get(Memory, memory_id)
    if not mem or mem.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="memory not found")
    await session.delete(mem)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
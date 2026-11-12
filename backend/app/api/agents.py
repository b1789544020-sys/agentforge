"""CRUD endpoints for :class:`Agent` records."""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import _auth_required, get_principal

from app.agent.definitions import (
    apply_definition,
    definition_response,
    resolve_definition,
    snapshot_dict,
)
from app.agent.exporter import (
    CANONICAL_FORMAT_VERSION,
    FORMAT_VERSION,
    agents_from_payload,
    build_payload,
    detect_format,
    dump as dump_payload,
    load as load_payload,
)
from app.agent.imports import (
    AgentNameConflictError,
    preflight_canonical_import,
    write_canonical_import,
)
from app.agent.memory import MemoryItem, MemoryStore
from app.agent.schema import (
    ResourceMap,
    masked_secret_report,
    public_definition,
    public_legacy_dto,
)
from app.agent.versions import create_agent_version, latest_agent_version
from app.audit import record_audit
from app.agent.tools import ToolRegistry
from app.api.schemas import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    DefinitionSaveRequest,
    ExportRequest,
    MemoryIn,
    MemoryOut,
    ToolOut,
)
from app.database import get_session
from app.dispatch import submit_gate
from app.gates.service import create_automatic_gates
from app.models import Agent, Memory
from app.rbac import accessible_team_ids, agent_scope_condition, resolve_team_for_create
from app.secrets import SecretPathConflictError, protect_config

router = APIRouter(prefix="/agents", tags=["agents"])


def _owner_filter(request: Request) -> str | None:
    """Return the non-admin owner id used to scope bulk Agent queries."""
    if not _auth_required():
        return None
    principal = get_principal(request)
    return None if principal.role == "admin" else principal.user_id


async def _agent_query_scope(request: Request, session: AsyncSession):
    teams = await accessible_team_ids(session, request)
    return agent_scope_condition(teams, get_principal(request).user_id)


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
    return public_legacy_dto({
        "id": agent.id,
        "team_id": agent.team_id,
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
    })


@router.get("", response_model=list[AgentOut])
async def list_agents(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    stmt = select(Agent).order_by(Agent.created_at.desc())
    scope = await _agent_query_scope(request, session)
    if scope is not None:
        stmt = stmt.where(scope)
    rows = (await session.execute(stmt)).scalars().all()
    return [_serialize_agent(a) for a in rows]


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate, request: Request, background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    team_id = await resolve_team_for_create(request, session, payload.team_id)
    agent = Agent(
        id=str(uuid.uuid4()),
        owner_id=_owner_filter(request),
        team_id=team_id,
        name=payload.name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        model=payload.model,
        provider=payload.provider,
        temperature=payload.temperature,
        max_steps=payload.max_steps,
        tools=json.dumps(payload.tools, ensure_ascii=False),
        config=json.dumps(protect_config(payload.config), ensure_ascii=False),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(agent)
    await session.flush()
    version = await create_agent_version(
        session, agent, created_by=get_principal(request).user_id
    )
    record_audit(session, request, action="agent.create", target_type="agent",
                 target_id=agent.id, team_id=agent.team_id,
                 detail={"version_id": version.id, "name": agent.name})

    await session.commit()
    gates = await create_automatic_gates(
        session, version, triggered_by=get_principal(request).user_id
    )
    for gate in gates:
        submit_gate(gate.id, background)
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


@router.get("/{agent_id}/definition")
async def get_current_definition(
    agent_id: str, session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    version = await latest_agent_version(session, agent_id)
    if version is None:
        raise HTTPException(status_code=409, detail="agent has no saved version")
    return definition_response(version)


@router.put("/{agent_id}/definition")
async def put_current_definition(
    agent_id: str,
    payload: DefinitionSaveRequest,
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    await session.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update())
    latest = await latest_agent_version(session, agent_id)
    if latest is None or latest.id != payload.base_version_id:
        raise HTTPException(status_code=409, detail={
            "error": "stale_base_version",
            "base_version_id": payload.base_version_id,
            "latest_version_id": latest.id if latest is not None else None,
            "latest_version_number": latest.version_number if latest is not None else None,
        })
    previous_snapshot = snapshot_dict(latest)
    try:
        resolved = await resolve_definition(
            session, agent, payload.definition,
            resource_map=payload.resource_map,
            secret_inputs=payload.secret_inputs,
            previous_config=json.loads(agent.config or "{}"),
            previous_snapshot=previous_snapshot,
        )
    except SecretPathConflictError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_definition", "message": str(exc),
        }) from exc
    if not resolved.ok:
        raise HTTPException(status_code=422, detail={
            "error": "resource_preflight_failed",
            "resolved": resolved.resolved,
            "unresolved": resolved.unresolved,
            "unsupported_tools": resolved.unsupported_tools,
        })
    try:
        await apply_definition(session, agent, resolved)
        agent.updated_at = datetime.utcnow()
        version = await create_agent_version(
            session, agent, created_by=get_principal(request).user_id,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail={
            "error": "resource_unavailable", "message": str(exc),
        }) from exc
    record_audit(
        session, request, action="agent.definition.update", target_type="agent",
        target_id=agent.id, team_id=agent.team_id,
        detail={"version_id": version.id, "base_version_id": latest.id},
    )
    await session.commit()
    gates = await create_automatic_gates(
        session, version, triggered_by=get_principal(request).user_id,
    )
    for gate in gates:
        submit_gate(gate.id, background)
    response = definition_response(version)
    response["resolved"] = public_legacy_dto(resolved.resolved)
    response["inherited_secrets"] = resolved.inherited_secrets
    return response


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    request: Request,
    background: BackgroundTasks,
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
        agent.config = json.dumps(
            protect_config(data["config"], json.loads(agent.config or "{}")), ensure_ascii=False
        )
        data.pop("config")
    for k, v in data.items():
        setattr(agent, k, v)
    agent.updated_at = datetime.utcnow()
    await session.flush()
    version = await create_agent_version(
        session, agent, created_by=get_principal(request).user_id
    )
    record_audit(session, request, action="agent.update", target_type="agent",
                 target_id=agent.id, team_id=agent.team_id,
                 detail={"version_id": version.id, "fields": sorted(data)})
    await session.commit()
    gates = await create_automatic_gates(
        session, version, triggered_by=get_principal(request).user_id
    )
    for gate in gates:
        submit_gate(gate.id, background)
    await session.refresh(agent)
    return _serialize_agent(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: str, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    record_audit(session, request, action="agent.delete", target_type="agent",
                 target_id=agent.id, team_id=agent.team_id, detail={"name": agent.name})
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


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {key: error[key] for key in ("type", "loc", "msg") if key in error}
        for error in exc.errors(include_url=False, include_input=False)
    ]


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
    request: Request,
    payload: ExportRequest | None = None,
    fmt: str = Query(default="json"),
    envelope: int = Query(default=CANONICAL_FORMAT_VERSION),
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
    scope = await _agent_query_scope(request, session)
    if agent_ids is None:
        stmt = select(Agent).order_by(Agent.created_at.desc())
        if scope is not None:
            stmt = stmt.where(scope)
        rows = (await session.execute(stmt)).scalars().all()
    else:
        if not agent_ids:
            raise HTTPException(
                status_code=400, detail="agent_ids must be non-empty if provided"
            )
        stmt = select(Agent).where(Agent.id.in_(agent_ids))
        if scope is not None:
            stmt = stmt.where(scope)
        rows = (await session.execute(stmt)).scalars().all()
        found = {a.id for a in rows}
        missing = [aid for aid in agent_ids if aid not in found]
        if missing:
            raise HTTPException(
                status_code=404,
                detail={"error": "agents not found", "missing_ids": missing},
            )

    if envelope not in {FORMAT_VERSION, CANONICAL_FORMAT_VERSION}:
        raise HTTPException(status_code=400, detail="envelope must be 1 or 2")
    export_rows: list[Any] = list(rows)
    if envelope == CANONICAL_FORMAT_VERSION:
        export_rows = []
        for agent in rows:
            version = await latest_agent_version(session, agent.id)
            export_rows.append(public_definition(snapshot_dict(version)) if version is not None else agent)
    payload_out = build_payload(export_rows, envelope=envelope)
    text = dump_payload(payload_out, fmt)
    filename = (
        f"agentforge-agents-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.{fmt}"
    )
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if envelope == FORMAT_VERSION:
        headers["X-AgentForge-Export-Warning"] = (
            "legacy envelope omits canonical resources, extensions, and secret metadata"
        )
    return Response(content=text, media_type=_MEDIA_TYPES[fmt], headers=headers)


@router.post("/import", response_model=dict)
async def import_agents(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    on_conflict: str = Form("skip"),
    fmt: str | None = Form(None),
    resource_map: str = Form("{}"),
    secret_inputs: str = Form("{}"),
    dry_run: bool = Query(False),
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
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_definition", "errors": _validation_errors(exc),
        }) from exc
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    owner_id = _owner_filter(request)
    scope = await _agent_query_scope(request, session)
    team_id = await resolve_team_for_create(request, session, None)

    if payload.get("version") == CANONICAL_FORMAT_VERSION:
        try:
            raw_map = json.loads(resource_map)
            raw_inputs = json.loads(secret_inputs)
            if not isinstance(raw_inputs, dict) or any(
                not isinstance(key, str) or not isinstance(value, str | type(None))
                for key, value in raw_inputs.items()
            ):
                raise ValueError("secret_inputs must be an object of string/null values")
            mapping = ResourceMap.model_validate(raw_map)
            preflight = await preflight_canonical_import(
                session, rows, on_conflict=on_conflict, owner_id=owner_id,
                team_id=team_id, scope=scope, resource_map=mapping,
                secret_inputs=raw_inputs,
            )
        except AgentNameConflictError as exc:
            raise HTTPException(status_code=409, detail={
                "error": "agent name already exists", "name": exc.name,
            }) from exc
        except SecretPathConflictError:
            raise
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail={
                "error": "invalid_resource_map", "errors": _validation_errors(exc),
            }) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={
                "error": "invalid_import_options", "message": str(exc),
            }) from exc
        report = preflight.report
        report["format"] = detected
        report["dry_run"] = dry_run
        if dry_run:
            return public_legacy_dto(report)
        if not report["ok"]:
            raise HTTPException(status_code=422, detail={
                "error": "resource_preflight_failed", **public_legacy_dto(report),
            })
        try:
            written = await write_canonical_import(
                session, preflight, created_by=get_principal(request).user_id,
            )
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=422, detail={
                "error": "resource_unavailable", "message": str(exc),
            }) from exc
        for plan, version in written:
            record_audit(
                session, request, action="agent.import", target_type="agent",
                target_id=plan.agent.id, team_id=plan.agent.team_id,
                detail={"version_id": version.id, "action": plan.action},
            )
        await session.commit()
        for _, version in written:
            gates = await create_automatic_gates(
                session, version, triggered_by=get_principal(request).user_id,
            )
            for gate in gates:
                submit_gate(gate.id, background)
        return public_legacy_dto(report)

    plans: list[tuple[dict[str, Any], Agent | None, str, dict[str, Any]]] = []
    missing_secrets: list[dict[str, Any]] = []
    inherited_secrets: list[dict[str, Any]] = []
    for row in rows:
        name = row["name"]
        stmt = select(Agent).where(Agent.name == name)
        if scope is not None:
            stmt = stmt.where(scope)
        existing = (await session.execute(stmt)).scalars().first()
        if existing is not None and on_conflict == "error":
            raise HTTPException(
                status_code=409,
                detail={"error": "agent name already exists", "name": name},
            )
        action = "create" if existing is None else (
            "skip" if on_conflict == "skip" else "update"
        )
        previous = json.loads(existing.config or "{}") if existing is not None else None
        protected = {} if action == "skip" else protect_config(row.get("config") or {}, previous)
        if action != "skip":
            report = masked_secret_report(row.get("config") or {}, previous)
            missing_secrets.extend({**item, "agent": name} for item in report["missing_secrets"])
            inherited_secrets.extend(
                {**item, "agent": name} for item in report["inherited_secrets"]
            )
        plans.append((row, existing, action, protected))

    created = [row["name"] for row, _, action, _ in plans if action == "create"]
    updated = [row["name"] for row, _, action, _ in plans if action == "update"]
    skipped = [row["name"] for row, _, action, _ in plans if action == "skip"]
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "format": detected,
            "on_conflict": on_conflict,
            "imported": len(created),
            "updated": len(updated),
            "skipped": len(skipped),
            "created": created,
            "updated_list": updated,
            "skipped_list": skipped,
            "missing_secrets": missing_secrets,
            "inherited_secrets": inherited_secrets,
        }

    versions: list[Any] = []
    for row, existing, action, protected in plans:
        if action == "skip":
            continue
        name = row["name"]
        if existing is not None:
            existing.description = row.get("description")
            existing.system_prompt = row.get("system_prompt")
            existing.model = row.get("model", existing.model)
            existing.provider = row.get("provider", existing.provider)
            existing.temperature = row.get("temperature", existing.temperature)
            existing.max_steps = row.get("max_steps", existing.max_steps)
            existing.tools = json.dumps(row.get("tools") or [], ensure_ascii=False)
            existing.config = json.dumps(protected, ensure_ascii=False)
            existing.updated_at = datetime.utcnow()
            await session.flush()
            versions.append(await create_agent_version(
                session, existing, created_by=get_principal(request).user_id
            ))
            continue

        agent = Agent(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            team_id=team_id,
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
            config=json.dumps(protected, ensure_ascii=False),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(agent)
        await session.flush()
        versions.append(await create_agent_version(
            session, agent, created_by=get_principal(request).user_id
        ))

    await session.commit()
    for version in versions:
        gates = await create_automatic_gates(
            session, version, triggered_by=get_principal(request).user_id
        )
        for gate in gates:
            submit_gate(gate.id, background)
    return {
        "ok": True,
        "dry_run": False,
        "format": detected,
        "on_conflict": on_conflict,
        "imported": len(created),
        "updated": len(updated),
        "skipped": len(skipped),
        "created": created,
        "updated_list": updated,
        "skipped_list": skipped,
        "missing_secrets": missing_secrets,
        "inherited_secrets": inherited_secrets,
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


def _memory_payload(item: MemoryItem, agent_id: str, score: float | None = None) -> dict[str, Any]:
    meta = item.meta
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (TypeError, ValueError):
            meta = {}
    return {
        "id": item.id,
        "agent_id": agent_id,
        "content": item.content,
        "kind": item.kind,
        "importance": item.importance,
        "created_at": item.created_at,
        "meta": meta if isinstance(meta, dict) else {},
        "score": score,
    }


@memory_router.get("", response_model=list[MemoryOut])
async def list_memory(
    agent_id: str,
    q: str | None = Query(default=None),
    query: str | None = Query(default=None),
    top_k: int = Query(default=50, ge=1, le=100),
    mode: str = Query(default="exact"),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    store = MemoryStore(session, agent_id=agent_id)
    text = q if q is not None else query
    if text is None or not text.strip():
        rows = await store.list(limit=top_k)
        return [_memory_payload(item, agent_id) for item in rows]
    aliases = {"text": "exact", "semantic": "embed", "embed": "embed", "exact": "exact", "hybrid": "hybrid"}
    if mode not in aliases:
        raise HTTPException(status_code=400, detail="mode must be exact, semantic/embed, or hybrid")
    hits = await store.search(text, limit=top_k, mode=aliases[mode])
    return [_memory_payload(item, agent_id, score) for item, score in hits]


@memory_router.post("", response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
async def add_memory(
    agent_id: str,
    payload: MemoryIn,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    item = await MemoryStore(session, agent_id=agent_id).add(
        payload.content,
        kind=payload.kind,
        importance=payload.importance,
        meta=payload.metadata,
    )
    return _memory_payload(item, agent_id)


@memory_router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    agent_id: str,
    memory_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    mem = await session.get(Memory, memory_id)
    if not mem or mem.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="memory not found")
    await MemoryStore(session, agent_id=agent_id).delete(memory_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
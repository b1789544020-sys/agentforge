"""Private knowledge-base management, upload, search, and Agent binding."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.versions import create_agent_version
from app.api.auth import get_principal
from app.audit import record_audit
from app.config import get_settings
from app.database import get_session
from app.dispatch import submit_gate
from app.gates.service import create_automatic_gates
from app.knowledge import extract_document, index_document, search_knowledge
from app.models import Agent, KnowledgeBase, KnowledgeBinding, KnowledgeChunk, KnowledgeDocument
from app.rbac import accessible_team_ids, authorize, resolve_team_for_create, team_role

router = APIRouter(tags=["knowledge"])


class KnowledgeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    team_id: str | None = None


def _kb_out(row: KnowledgeBase) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "description": row.description,
            "team_id": row.team_id, "owner_id": row.owner_id, "created_at": row.created_at}


def _doc_out(row: KnowledgeDocument, chunks: int = 0) -> dict[str, Any]:
    return {"id": row.id, "kb_id": row.kb_id, "filename": row.filename,
            "media_type": row.media_type, "status": row.status, "error": row.error,
            "chunks": chunks, "created_at": row.created_at}


async def _access(request: Request, session: AsyncSession, row: KnowledgeBase | None, write: bool = False) -> KnowledgeBase:
    if row is None:
        raise HTTPException(404, "knowledge base not found")
    if get_principal(request).role != "admin":
        if row.team_id:
            if await team_role(session, get_principal(request).user_id or "", row.team_id) is None:
                raise HTTPException(404, "knowledge base not found")
            if write:
                await authorize(request, session, "memory:write", row.team_id)
        elif row.owner_id != get_principal(request).user_id:
            raise HTTPException(404, "knowledge base not found")
    return row


@router.post("/knowledge-bases", status_code=201)
async def create_kb(payload: KnowledgeCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    team_id = await resolve_team_for_create(request, session, payload.team_id)
    row = KnowledgeBase(name=payload.name.strip(), description=payload.description,
                        team_id=team_id, owner_id=get_principal(request).user_id)
    session.add(row)
    await session.flush()
    record_audit(session, request, action="knowledge.create", target_type="knowledge_base", target_id=row.id,
                 team_id=team_id, detail={"name": row.name})
    await session.commit()
    return _kb_out(row)


@router.get("/knowledge-bases")
async def list_kbs(request: Request, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())
    teams = await accessible_team_ids(session, request)
    if teams is not None:
        stmt = stmt.where(KnowledgeBase.team_id.in_(teams)) if teams else stmt.where(
            KnowledgeBase.owner_id == get_principal(request).user_id
        )
    return [_kb_out(row) for row in (await session.execute(stmt)).scalars().all()]


@router.post("/knowledge-bases/{kb_id}/documents", status_code=201)
async def upload_document(kb_id: str, request: Request, file: UploadFile = File(...), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    kb = await _access(request, session, await session.get(KnowledgeBase, kb_id), write=True)
    raw = await file.read(get_settings().upload_max_bytes + 1)
    if len(raw) > get_settings().upload_max_bytes:
        raise HTTPException(413, "document exceeds upload_max_bytes")
    document = KnowledgeDocument(kb_id=kb.id, filename=file.filename or "unnamed",
                                 media_type=file.content_type or "application/octet-stream",
                                 content="", status="processing")
    session.add(document)
    await session.flush()
    try:
        document.content = extract_document(document.filename, raw)
        if not document.content.strip():
            raise ValueError("document contains no extractable text")
        chunks = await index_document(session, document)
        document.status = "ready"
    except ValueError as exc:
        document.status = "failed"
        document.error = str(exc)
        await session.commit()
        raise HTTPException(400, str(exc)) from exc
    record_audit(session, request, action="knowledge.upload", target_type="knowledge_document", target_id=document.id,
                 team_id=kb.team_id, detail={"filename": document.filename, "chunks": len(chunks)})
    await session.commit()
    return _doc_out(document, len(chunks))


@router.get("/knowledge-bases/{kb_id}/documents")
async def list_documents(kb_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    await _access(request, session, await session.get(KnowledgeBase, kb_id))
    rows = (await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id)
                                  .order_by(KnowledgeDocument.created_at.desc()))).scalars().all()
    output = []
    for row in rows:
        count = len((await session.execute(select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == row.id))).scalars().all())
        output.append(_doc_out(row, count))
    return output


@router.get("/knowledge-bases/{kb_id}/search")
async def search_kb(kb_id: str, agent_id: str, q: str, request: Request, top_k: int = 5, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    kb = await _access(request, session, await session.get(KnowledgeBase, kb_id))
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.team_id != kb.team_id:
        raise HTTPException(404, "agent not found in this knowledge base team")
    return await search_knowledge(session, agent_id, q, kb_ids=[kb.id], limit=max(1, min(top_k, 20)))


@router.put("/agents/{agent_id}/knowledge/{kb_id}", status_code=201)
async def bind_kb(
    agent_id: str, kb_id: str, request: Request, background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    kb = await _access(request, session, await session.get(KnowledgeBase, kb_id), write=True)
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.team_id != kb.team_id:
        raise HTTPException(404, "agent not found in this knowledge base team")
    existing = (await session.execute(select(KnowledgeBinding).where(
        KnowledgeBinding.agent_id == agent_id, KnowledgeBinding.kb_id == kb_id
    ))).scalar_one_or_none()
    saved_version = None
    if existing is None:
        existing = KnowledgeBinding(agent_id=agent_id, kb_id=kb_id)
        session.add(existing)
        await session.flush()
        agent.updated_at = datetime.utcnow()
        saved_version = await create_agent_version(
            session, agent, created_by=get_principal(request).user_id
        )
    record_audit(session, request, action="knowledge.bind", target_type="knowledge_binding", target_id=existing.id,
                 team_id=kb.team_id, detail={"agent_id": agent_id, "kb_id": kb_id})
    await session.commit()
    if saved_version is not None:
        gates = await create_automatic_gates(
            session, saved_version, triggered_by=get_principal(request).user_id
        )
        for gate in gates:
            submit_gate(gate.id, background)
    return {
        "agent_id": agent_id, "kb_id": kb_id, "binding_id": existing.id,
        "agent_version_id": saved_version.id if saved_version is not None else None,
    }


@router.delete("/agents/{agent_id}/knowledge/{kb_id}", status_code=204, response_model=None)
async def unbind_kb(
    agent_id: str, kb_id: str, request: Request, background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> None:
    kb = await _access(request, session, await session.get(KnowledgeBase, kb_id), write=True)
    binding = (await session.execute(select(KnowledgeBinding).where(
        KnowledgeBinding.agent_id == agent_id, KnowledgeBinding.kb_id == kb_id
    ))).scalar_one_or_none()
    if binding:
        record_audit(session, request, action="knowledge.unbind", target_type="knowledge_binding", target_id=binding.id,
                     team_id=kb.team_id, detail={"agent_id": agent_id, "kb_id": kb_id})
        agent = await session.get(Agent, agent_id)
        if agent is None:
            raise HTTPException(404, "agent not found")
        await session.delete(binding)
        await session.flush()
        agent.updated_at = datetime.utcnow()
        saved_version = await create_agent_version(
            session, agent, created_by=get_principal(request).user_id
        )
        await session.commit()
        gates = await create_automatic_gates(
            session, saved_version, triggered_by=get_principal(request).user_id
        )
        for gate in gates:
            submit_gate(gate.id, background)


__all__ = ["router"]

"""Document ingestion, Chinese-friendly chunks, and scoped knowledge retrieval."""
from __future__ import annotations

import hashlib
import io
import re
import time
import uuid
import zipfile
from typing import Any
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.memory.embeddings import encode, get_default_embedder, pack_vector, unpack_vector
from app.memory.vector_store import get_vector_store
from app.models import Agent, KnowledgeBase, KnowledgeBinding, KnowledgeChunk, KnowledgeDocument

_SENTENCE_END = re.compile(r"(?<=[。！？；!?;])\s*")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")


def extract_document(filename: str, raw: bytes) -> str:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix in {"txt", "md", "markdown"}:
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("text/Markdown files must be UTF-8") from exc
    if suffix in {"docx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                root = ElementTree.fromstring(archive.read("word/document.xml"))
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            return "\n\n".join("".join(node.text or "" for node in paragraph.iter(ns + "t"))
                                for paragraph in root.iter(ns + "p"))
        except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise ValueError("invalid DOCX document") from exc
    if suffix == "pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:
                raise ValueError("encrypted PDF is not supported")
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"invalid PDF document: {exc}") from exc
    raise ValueError("supported file types: PDF, DOCX, TXT, Markdown")


def chinese_chunks(text: str, max_chars: int = 800) -> list[dict[str, Any]]:
    """Preserve section/paragraph/sentence boundaries instead of fixed cuts."""
    output: list[dict[str, Any]] = []
    section: str | None = None
    for raw_paragraph in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        paragraph = raw_paragraph.strip()
        if not paragraph:
            continue
        heading = _HEADING.match(paragraph)
        if heading and len(paragraph.splitlines()) == 1:
            section = heading.group(1).strip()
            continue
        sentences = [item.strip() for item in _SENTENCE_END.split(paragraph) if item.strip()]
        current = ""
        for sentence in sentences:
            while len(sentence) > max_chars:
                if current:
                    output.append({"content": current, "section": section})
                    current = ""
                # Last resort only for a single sentence with no usable punctuation.
                output.append({"content": sentence[:max_chars], "section": section})
                sentence = sentence[max_chars:]
            candidate = f"{current}\n{sentence}" if current else sentence
            if current and len(candidate) > max_chars:
                output.append({"content": current, "section": section})
                current = sentence
            else:
                current = candidate
        if current:
            output.append({"content": current, "section": section})
    return output


async def index_document(session: AsyncSession, document: KnowledgeDocument) -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    vectors: list[list[float]] = []
    for index, raw in enumerate(chinese_chunks(document.content)):
        vector = encode(raw["content"])
        chunk = KnowledgeChunk(
            document_id=document.id, kb_id=document.kb_id, chunk_index=index,
            content=raw["content"], section=raw["section"], embedding=pack_vector(vector),
        )
        session.add(chunk)
        chunks.append(chunk)
        vectors.append(vector)
    await session.flush()
    if chunks:
        get_vector_store().upsert([chunk.id for chunk in chunks], vectors)
    return chunks


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return float(sum(a * b for a, b in zip(left, right, strict=True)))


def _hit(chunk: KnowledgeChunk, document: KnowledgeDocument, score: float) -> dict[str, Any]:
    return {
        "content": chunk.content, "section": chunk.section, "document": document.filename,
        "chunk_id": chunk.id, "score": score, "kb_id": chunk.kb_id,
        "document_id": document.id,
        "document_sha256": hashlib.sha256(document.content.encode("utf-8")).hexdigest(),
        "chunk_sha256": hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
    }


async def search_knowledge(
    session: AsyncSession, agent_id: str, query: str, *, kb_ids: list[str] | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    agent = await session.get(Agent, agent_id)
    if agent is None or not query.strip():
        return []
    if kb_ids is None:
        kb_ids = list((await session.execute(
            select(KnowledgeBinding.kb_id).where(KnowledgeBinding.agent_id == agent_id)
        )).scalars().all())
    if not kb_ids:
        return []
    scope = select(KnowledgeBase.id).where(KnowledgeBase.id.in_(kb_ids))
    if agent.team_id:
        scope = scope.where(KnowledgeBase.team_id == agent.team_id)
    else:
        scope = scope.where(KnowledgeBase.team_id.is_(None),
                            (KnowledgeBase.owner_id == agent.owner_id) | KnowledgeBase.owner_id.is_(None))
    allowed_ids = set((await session.execute(scope)).scalars().all())
    if not allowed_ids:
        return []
    vector = encode(query)
    hits = get_vector_store().search(vector, k=max(limit * 4, limit))
    hit_ids = [hit.id for hit in hits]
    if hit_ids:
        rows = (await session.execute(
            select(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(KnowledgeChunk.id.in_(hit_ids), KnowledgeChunk.kb_id.in_(allowed_ids))
        )).all()
        by_id = {chunk.id: (chunk, document) for chunk, document in rows}
        results = [
            _hit(by_id[hit.id][0], by_id[hit.id][1], hit.score)
            for hit in hits if hit.id in by_id
        ]
        if results:
            return results[:limit]
    # SQL fallback also rebuilds the in-process path after a restart.
    rows = (await session.execute(
        select(KnowledgeChunk, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(KnowledgeChunk.kb_id.in_(allowed_ids))
    )).all()
    ranked = [
        _hit(chunk, document, _cosine(vector, unpack_vector(chunk.embedding)))
        for chunk, document in rows
    ]
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]


async def _allowed_kb_ids(
    session: AsyncSession, agent_id: str, kb_ids: list[str] | None,
) -> list[str]:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        return []
    requested = kb_ids
    if requested is None:
        requested = list((await session.execute(
            select(KnowledgeBinding.kb_id).where(KnowledgeBinding.agent_id == agent_id)
        )).scalars().all())
    if not requested:
        return []
    scope = select(KnowledgeBase.id).where(KnowledgeBase.id.in_(requested))
    if agent.team_id:
        scope = scope.where(KnowledgeBase.team_id == agent.team_id)
    else:
        scope = scope.where(
            KnowledgeBase.team_id.is_(None),
            (KnowledgeBase.owner_id == agent.owner_id) | KnowledgeBase.owner_id.is_(None),
        )
    return sorted((await session.execute(scope)).scalars().all())


async def knowledge_context_with_quality(
    session: AsyncSession, agent_id: str, query: str, *, kb_ids: list[str] | None = None,
    limit: int = 5,
) -> tuple[str, dict[str, Any]]:
    """Return the exact injected context and its corresponding retrieval observation."""
    started = time.perf_counter()
    hits = await search_knowledge(session, agent_id, query, kb_ids=kb_ids, limit=limit)
    allowed_ids = await _allowed_kb_ids(session, agent_id, kb_ids)
    empty = {"queries": [], "sources": []}
    if not allowed_ids:
        return "", empty
    corpus_rows = (await session.execute(
        select(KnowledgeChunk.id, KnowledgeChunk.content)
        .where(KnowledgeChunk.kb_id.in_(allowed_ids))
    )).all()
    manifest = hashlib.sha256()
    for chunk_id, content in sorted(corpus_rows, key=lambda row: row[0]):
        manifest.update(chunk_id.encode("utf-8"))
        manifest.update(b"\0")
        manifest.update(content.encode("utf-8"))
        manifest.update(b"\0")
    manifest_sha = manifest.hexdigest()
    settings = get_settings()
    embedder = get_default_embedder(dim=settings.embedding_dim)
    provider = settings.embedding_provider
    observation = {
        "queries": [{
            "query_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentforge:{agent_id}:{query}:{manifest_sha}")),
            "text": query, "knowledge_base_ids": allowed_ids,
            "embedding": {
                "provider": provider,
                "model": "hashing" if provider == "hashing" else settings.embedding_model,
                "dimensions": embedder.dim,
            },
            "index": {
                "snapshot_id": f"corpus-{manifest_sha[:16]}",
                "algorithm": f"{settings.vector_backend}-or-sql-cosine-v1",
                "corpus_manifest_sha256": manifest_sha,
            },
            "top_k": limit, "filters": {"kb_scope": "authorized"},
            "candidates": [{
                "source_id": hit["chunk_id"], "rank": rank,
                "score": float(hit["score"]), "injected": True,
            } for rank, hit in enumerate(hits, 1)],
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "status": "completed", "error": None,
        }],
        "sources": [{
            "source_id": hit["chunk_id"], "knowledge_base_id": hit["kb_id"],
            "document_id": hit["document_id"], "chunk_id": hit["chunk_id"],
            "document_sha256": hit["document_sha256"], "chunk_sha256": hit["chunk_sha256"],
            "locator": {"filename": hit["document"], "section": hit.get("section")},
            "source_excerpt": hit["content"][:4000],
            "excerpt_truncated": len(hit["content"]) > 4000,
        } for hit in hits],
    }
    if not hits:
        return "", observation
    lines = ["以下是用户私有知识库检索结果。只能依据这些编号来源回答，并在适用处保留引用编号："]
    for index, hit in enumerate(hits, 1):
        source = hit["document"] + (f"#{hit['section']}" if hit.get("section") else "")
        lines.append(f"[{index}] 来源：{source}\n{hit['content']}")
    return "\n\n".join(lines), observation


async def knowledge_context(
    session: AsyncSession, agent_id: str, query: str, *, kb_ids: list[str] | None = None,
) -> str:
    context, _ = await knowledge_context_with_quality(session, agent_id, query, kb_ids=kb_ids)
    return context


__all__ = [
    "chinese_chunks",
    "extract_document",
    "index_document",
    "knowledge_context",
    "knowledge_context_with_quality",
    "search_knowledge",
]

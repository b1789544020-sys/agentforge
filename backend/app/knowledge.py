"""Document ingestion, Chinese-friendly chunks, and scoped knowledge retrieval."""
from __future__ import annotations

import io
import re
import zipfile
from typing import Any
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.embeddings import encode, pack_vector, unpack_vector
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
            select(KnowledgeChunk, KnowledgeDocument.filename)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(KnowledgeChunk.id.in_(hit_ids), KnowledgeChunk.kb_id.in_(allowed_ids))
        )).all()
        by_id = {chunk.id: (chunk, filename) for chunk, filename in rows}
        results = [{"content": by_id[hit.id][0].content, "section": by_id[hit.id][0].section,
                    "document": by_id[hit.id][1], "chunk_id": hit.id, "score": hit.score}
                   for hit in hits if hit.id in by_id]
        if results:
            return results[:limit]
    # SQL fallback also rebuilds the in-process path after a restart.
    rows = (await session.execute(
        select(KnowledgeChunk, KnowledgeDocument.filename)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(KnowledgeChunk.kb_id.in_(allowed_ids))
    )).all()
    ranked = [{"content": chunk.content, "section": chunk.section, "document": filename,
               "chunk_id": chunk.id, "score": _cosine(vector, unpack_vector(chunk.embedding))}
              for chunk, filename in rows]
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]


async def knowledge_context(
    session: AsyncSession, agent_id: str, query: str, *, kb_ids: list[str] | None = None,
) -> str:
    hits = await search_knowledge(session, agent_id, query, kb_ids=kb_ids)
    if not hits:
        return ""
    lines = ["以下是用户私有知识库检索结果。请优先依据这些内容回答，并在适用处保留引用编号："]
    for index, hit in enumerate(hits, 1):
        source = hit["document"] + (f"#{hit['section']}" if hit.get("section") else "")
        lines.append(f"[{index}] 来源：{source}\n{hit['content']}")
    return "\n\n".join(lines)


__all__ = ["extract_document", "chinese_chunks", "index_document", "search_knowledge", "knowledge_context"]

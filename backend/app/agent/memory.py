"""Long-term memory system.

The :class:`MemoryStore` is a thin wrapper around the :class:`Memory`
ORM model. It supports two retrieval strategies:

* ``exact`` – substring match on the ``content`` field.
* ``embed`` – cosine similarity on stored ``embedding`` vectors
  resolved through :class:`app.memory.vector_store.VectorStore`.

Starting with 0.1.2 the embed path is real: writes go through
:func:`app.memory.embeddings.encode` (a hashing-trick encoder, zero
external dependencies) and reads use the configured vector backend
(in-process by default).  Callers do not need to know the embedding
model exists; passing ``content`` is enough.
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Literal, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Memory
from app.llm.client import count_tokens
from app.memory.embeddings import (
    encode as _encode_text,
    pack_vector as _pack_vector,
    unpack_vector as _unpack_vector,
)

logger = logging.getLogger(__name__)

Mode = Literal["exact", "embed", "hybrid"]


def _not_expired_clause():
    """SQL filter that hides rows whose ``expires_at`` is in the past.

    ``NULL`` ``expires_at`` is treated as "never expires" and is
    always included.  We pass ``datetime.utcnow()`` once at call time
    so a single query sees a consistent cutoff.
    """
    now = datetime.utcnow()
    return (Memory.expires_at.is_(None)) | (Memory.expires_at > now)


@dataclass
class MemoryItem:
    """Public-facing memory record."""

    id: str
    content: str
    kind: str
    importance: float
    created_at: datetime
    meta: dict[str, Any]
    # TTL bookkeeping (0.1.2 4.3).  ``None`` = never expires.
    expires_at: datetime | None = None

    @classmethod
    def from_orm(cls, row: Memory) -> "MemoryItem":
        return cls(
            id=row.id,
            content=row.content,
            kind=row.kind,
            importance=row.importance,
            created_at=row.created_at,
            meta=row.meta or {},
            expires_at=row.expires_at,
        )


async def cleanup_expired(
    session: AsyncSession, *, batch: int = 200, agent_id: str | None = None
) -> int:
    """Delete rows whose ``expires_at`` is in the past.

    Returns the number of rows removed.  The query is capped at
    ``batch`` to keep long-running DB transactions out of the way of
    live traffic; the periodic task can call this in a loop until it
    returns 0 if the user wants a full sweep.

    The vector index is mirrored best-effort so a deleted row does
    not survive as a tombstone in cosine results.
    """
    now = datetime.utcnow()
    stmt = select(Memory).where(Memory.expires_at.isnot(None)).where(
        Memory.expires_at <= now
    )
    if agent_id is not None:
        stmt = stmt.where(Memory.agent_id == agent_id)
    stmt = stmt.limit(batch)
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return 0
    ids = [r.id for r in rows]
    for row in rows:
        await session.delete(row)
    await session.commit()
    try:
        from app.memory.vector_store import get_vector_store

        get_vector_store().delete(ids)
    except Exception:  # pragma: no cover - defensive
        logger.debug("vector index cleanup delete failed", exc_info=True)
    return len(ids)


class MemoryStore:
    """Per-agent memory facade."""

    def __init__(self, session: AsyncSession, agent_id: str | None = None) -> None:
        self.session = session
        self.agent_id = agent_id

    # ---- write -------------------------------------------------------
    async def add(
        self,
        content: str,
        *,
        kind: str = "episodic",
        importance: float = 0.5,
        meta: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
        session_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> MemoryItem:
        """Insert a memory row and update the in-process vector index.

        If ``embedding`` is omitted the content is encoded on the fly
        via :func:`app.memory.embeddings.encode`.  Empty content
        produces a NULL embedding (and the row is not added to the
        vector index) so the same row can still be returned by exact
        search but will not poison cosine results with a zero vector.

        ``ttl_seconds`` (0.1.2 4.3) sets a per-row expiry.  ``None``
        means "never expires".  Expired rows are filtered out by
        :meth:`list` / :meth:`search` and deleted by
        :func:`cleanup_expired`.
        """
        # Resolve embedding -> blob.  The two encoders are mutually
        # exclusive: explicit kwarg wins, otherwise we encode content.
        settings = get_settings()
        if embedding is None and content:
            embedding = _encode_text(
                content,
                dim=settings.embedding_dim,
                max_tokens=settings.embedding_max_tokens,
            )
        embedding_blob: bytes | None = None
        if embedding:
            embedding_blob = _pack_vector(embedding)

        expires_at: datetime | None = None
        if ttl_seconds is not None and ttl_seconds > 0:
            expires_at = datetime.utcnow() + timedelta(seconds=ttl_seconds)

        row = Memory(
            id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            session_id=session_id,
            kind=kind,
            content=content,
            meta=meta or {},
            importance=importance,
            embedding=embedding_blob,
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)

        # Mirror the new row into the vector index.  The vector store
        # is process-global so we keep it in sync best-effort: a
        # failure here only means a missed cache hit, not a lost row.
        if embedding:
            try:
                from app.memory.vector_store import get_vector_store

                get_vector_store().upsert([row.id], [embedding])
            except Exception:  # pragma: no cover - defensive
                logger.debug("vector index upsert failed", exc_info=True)

        return MemoryItem.from_orm(row)

    # ---- read --------------------------------------------------------
    async def list(
        self, *, limit: int = 50, kind: str | None = None
    ) -> list[MemoryItem]:
        stmt = (
            select(Memory)
            .where(_not_expired_clause())
            .order_by(Memory.created_at.desc())
            .limit(limit)
        )
        if self.agent_id is not None:
            stmt = stmt.where(Memory.agent_id == self.agent_id)
        if kind is not None:
            stmt = stmt.where(Memory.kind == kind)
        result = await self.session.execute(stmt)
        return [MemoryItem.from_orm(r) for r in result.scalars().all()]

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        mode: Mode = "exact",
        query_embedding: list[float] | None = None,
    ) -> list[tuple[MemoryItem, float]]:
        """Return ``(item, score)`` tuples sorted by descending score.

        For ``mode="embed"`` (and the embed leg of ``"hybrid"``) the
        caller may either pass an explicit ``query_embedding`` or let
        the store encode the ``query`` string on the fly.  Explicit
        embeddings skip the encode call but are not normalised; the
        store will L2-normalise them on the way in.
        """
        if mode == "exact":
            return await self._search_exact(query, limit)
        # Resolve query embedding once for embed / hybrid paths so the
        # encode cost is paid at most once.
        if query_embedding is None and query:
            settings = get_settings()
            query_embedding = _encode_text(
                query,
                dim=settings.embedding_dim,
                max_tokens=settings.embedding_max_tokens,
            )
        if mode == "embed":
            return await self._search_embed(query_embedding or [], limit)
        # hybrid
        exact = await self._search_exact(query, limit=limit)
        embed = await self._search_embed(query_embedding or [], limit=limit)
        merged: dict[str, tuple[MemoryItem, float]] = {}
        for item, score in exact + embed:
            merged[item.id] = (item, max(merged.get(item.id, (item, 0.0))[1], score))
        return sorted(merged.values(), key=lambda kv: kv[1], reverse=True)[:limit]

    async def delete(self, memory_id: str) -> bool:
        row = await self.session.get(Memory, memory_id)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.commit()
        # Mirror the removal in the vector index so a later embed
        # search does not return a tombstone.
        try:
            from app.memory.vector_store import get_vector_store

            get_vector_store().delete([memory_id])
        except Exception:  # pragma: no cover - defensive
            logger.debug("vector index delete failed", exc_info=True)
        return True

    # ---- internals ---------------------------------------------------
    async def _search_exact(
        self, query: str, limit: int
    ) -> list[tuple[MemoryItem, float]]:
        if not query:
            return []
        stmt = (
            select(Memory)
            .where(_not_expired_clause())
            .order_by(Memory.created_at.desc())
            .limit(limit * 4)
        )
        if self.agent_id is not None:
            stmt = stmt.where(Memory.agent_id == self.agent_id)
        result = await self.session.execute(stmt)
        q = query.lower()
        scored: list[tuple[MemoryItem, float]] = []
        for row in result.scalars().all():
            content = (row.content or "").lower()
            if not content:
                continue
            score = 0.0
            if q in content:
                score += 0.6
            tokens = set(q.split())
            overlap = tokens & set(content.split())
            if overlap:
                score += 0.4 * len(overlap) / max(len(tokens), 1)
            score += float(row.importance) * 0.2
            if score > 0:
                scored.append((MemoryItem.from_orm(row), score))
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return scored[:limit]

    async def _search_embed(
        self, query_embedding: list[float], limit: int
    ) -> list[tuple[MemoryItem, float]]:
        """Cosine-similarity search through the configured vector backend.

        Returns ``(item, score)`` tuples sorted by descending score.  The
        result size is capped at ``limit`` *and* at the number of rows
        that actually have embeddings -- rows that were written before
        the migration or whose content could not be encoded are simply
        absent from this list.
        """
        if not query_embedding:
            return []
        # Lazy import so the vector backend can be swapped at runtime
        # without circular import issues between memory -> vector_store.
        from app.memory.vector_store import get_vector_store

        store = get_vector_store()
        # Over-fetch a little so we can re-rank by importance and
        # importance-tie-break still leaves us with ``limit`` rows.
        over_fetch = max(limit * 2, 10)
        hits = store.search(query_embedding, k=over_fetch)
        if not hits:
            return []

        # Hydrate the corresponding ORM rows in one round-trip.
        # The not-expired filter runs on the SQL side so an expired row
        # can never appear in the result, even if the vector index is
        # one cleanup cycle behind.
        ids = [h.id for h in hits]
        stmt = select(Memory).where(Memory.id.in_(ids)).where(_not_expired_clause())
        if self.agent_id is not None:
            stmt = stmt.where(Memory.agent_id == self.agent_id)
        result = await self.session.execute(stmt)
        rows_by_id: dict[str, Memory] = {r.id: r for r in result.scalars().all()}

        out: list[tuple[MemoryItem, float]] = []
        for hit in hits:
            row = rows_by_id.get(hit.id)
            if row is None:
                # Vector index has a stale id; drop it best-effort.
                try:
                    store.delete([hit.id])
                except Exception:  # pragma: no cover - defensive
                    pass
                continue
            # Importance acts as a small tie-breaker; we keep the cosine
            # score as the primary number so the caller can interpret
            # it consistently.
            adjusted = float(hit.score) + 0.001 * float(row.importance)
            out.append((MemoryItem.from_orm(row), adjusted))
        out.sort(key=lambda kv: kv[1], reverse=True)
        return out[:limit]


def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
    va = list(a)
    vb = list(b)
    if not va or not vb or len(va) != len(vb):
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


__all__ = ["MemoryStore", "MemoryItem", "cleanup_expired"]
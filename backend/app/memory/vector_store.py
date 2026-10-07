"""Vector store interface and the default in-process implementation.

Why an interface at all?  Two reasons:

1. The real production path is ChromaDB (or pgvector) running out of
   process; we want to swap the implementation without touching
   :class:`app.agent.memory.MemoryStore`.
2. The in-process store needs to be a *separate* object from the SQL
   session so that it can be cached, serialised for tests, and
   populated lazily as memory rows are added.

The in-process implementation here is intentionally minimal: a list
of ``(id, vector)`` plus a linear scan.  For 0.1.2 the agent rarely
holds more than a few hundred memories, so the asymptotic cost of
the linear scan is dwarfed by the SQLite round-trip.  When the
ChromaDB backend lands it will own its own ANN index.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from app.config import get_settings


@dataclass(frozen=True)
class VectorHit:
    """A single nearest-neighbour search result.

    ``score`` is cosine similarity in ``[-1.0, 1.0]`` (we normalise at
    encode time, so all real scores are in ``[0.0, 1.0]``).
    """

    id: str
    score: float


@runtime_checkable
class VectorStore(Protocol):
    """Minimal contract every vector backend must satisfy.

    Implementations are *not* required to persist state -- that is the
    job of the SQL layer.  These methods only maintain the in-memory
    index that :class:`app.agent.memory.MemoryStore` consults on a
    ``search`` call.
    """

    def upsert(
        self, ids: Sequence[str], vectors: Sequence[Sequence[float]]
    ) -> None: ...

    def delete(self, ids: Sequence[str]) -> None: ...

    def search(
        self, vector: Sequence[float], *, k: int = 5
    ) -> list[VectorHit]: ...

class InMemoryVectorStore:
    """Lock-protected dict-of-vectors backend.

    The lock guards a single ``_index: dict[str, tuple[float, ...]]``.
    We materialise the stored value as a tuple so that callers cannot
    accidentally mutate the index by holding a reference to the
    returned vector.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._index: dict[str, tuple[float, ...]] = {}

    def upsert(
        self,
        ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        if len(ids) != len(vectors):
            raise ValueError(
                f"ids and vectors length mismatch: {len(ids)} vs {len(vectors)}"
            )
        with self._lock:
            for id_, vec in zip(ids, vectors):
                if not vec:
                    self._index.pop(id_, None)
                    continue
                self._index[id_] = tuple(float(x) for x in vec)

    def delete(self, ids: Sequence[str]) -> None:
        with self._lock:
            for id_ in ids:
                self._index.pop(id_, None)

    def clear(self) -> None:
        """Test hook -- drop every vector.  Not part of the Protocol."""
        with self._lock:
            self._index.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._index)

    def search(
        self,
        vector: Sequence[float],
        *,
        k: int = 5,
    ) -> list[VectorHit]:
        if not vector:
            return []
        q = tuple(float(x) for x in vector)
        qn = math.sqrt(sum(x * x for x in q))
        if qn == 0:
            return []
        with self._lock:
            scored: list[VectorHit] = []
            for id_, vec in self._index.items():
                if not vec:
                    continue
                norm = math.sqrt(sum(x * x for x in vec))
                if norm == 0:
                    continue
                dot = sum(a * b for a, b in zip(q, vec))
                score = dot / (qn * norm)
                if score > 0:
                    scored.append(VectorHit(id=id_, score=float(score)))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:k]


_lock = threading.Lock()
_default_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    """Return the process-wide vector store.

    The choice between backends is controlled by
    ``AGENTFORGE_VECTOR_BACKEND``.  The in-process backend is the
    default and requires no extra services; the ChromaDB path is
    reserved for a future release.
    """
    global _default_store
    with _lock:
        if _default_store is None:
            settings = get_settings()
            backend = settings.vector_backend
            if backend == "inprocess":
                _default_store = InMemoryVectorStore()
            elif backend == "chroma":  # pragma: no cover
                raise NotImplementedError(
                    "AGENTFORGE_VECTOR_BACKEND=chroma is not wired up in 0.1.2; "
                    "use 'inprocess' or implement the ChromaDB client."
                )
            else:
                raise ValueError(
                    f"Unknown vector backend: {backend!r} "
                    "(expected 'inprocess' or 'chroma')"
                )
        return _default_store


def _reset_default_store() -> None:
    """Test hook -- drop the cached store so the next call rebuilds it."""
    global _default_store
    with _lock:
        _default_store = None


__all__ = [
    "VectorStore",
    "InMemoryVectorStore",
    "VectorHit",
    "get_vector_store",
    "_reset_default_store",
]


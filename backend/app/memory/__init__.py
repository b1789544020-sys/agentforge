"""Vector memory subsystem.

This package provides:

* :mod:`app.memory.embeddings` -- pure-Python text encoder producing
  fixed-dimension float vectors.  No third-party embedding model is
  required, so the system runs in CI / minimal installs without
  ``sentence-transformers`` or network access.
* :mod:`app.memory.vector_store` -- abstract ``VectorStore`` Protocol
  plus an in-process implementation backed by ``app.agent.memory``.
  A future ChromaDB backend is described in the docs but not wired
  in 0.1.2.
* :mod:`app.memory.cleanup` -- background task that periodically
  deletes rows whose ``expires_at`` has passed (0.1.2 4.3).

The :class:`app.agent.memory.MemoryStore` consumes both modules:
``embeddings.encode`` produces vectors at write-time and
``vector_store`` does the nearest-neighbour search.
"""
from app.memory.embeddings import (
    Embedder,
    HashingEmbedder,
    SentenceTransformerEmbedder,
    encode,
    pack_vector,
    unpack_vector,
)
from app.memory.vector_store import (
    InMemoryVectorStore,
    QdrantVectorStore,
    VectorHit,
    VectorStore,
    get_vector_store,
    _reset_default_store as _reset_default_vector_store,
)
from app.memory.embeddings import _reset_default_embedder
from app.memory.cleanup import (
    SessionFactory,
    schedule_periodic_cleanup,
)

__all__ = [
    "Embedder",
    "HashingEmbedder",
    "SentenceTransformerEmbedder",
    "encode",
    "pack_vector",
    "unpack_vector",
    "InMemoryVectorStore",
    "QdrantVectorStore",
    "VectorHit",
    "VectorStore",
    "get_vector_store",
    "_reset_default_embedder",
    "_reset_default_vector_store",
    "SessionFactory",
    "schedule_periodic_cleanup",
]


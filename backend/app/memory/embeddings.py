"""Pure-Python text embedder.

The :class:`HashingEmbedder` implements the "hashing trick" -- each
token is hashed to one of ``dim`` buckets and the resulting vector is
``sign(token) -> 1.0`` for that bucket.  This is dimension-stable,
language-agnostic and needs no trained weights, which is why we use
it for the default in-process vector backend.

The vectors are L2-normalised at encode time so cosine similarity
becomes a single dot product (see ``_cosine`` in
:mod:`app.agent.memory`).

Why not sentence-transformers?

* Adds ~200 MB of model weights and a torch dependency that the
  default Docker image does not include.
* Loading a model is slow enough to make unit tests flaky.
* Most AgentForge users store short, factual notes ("prefers
  Chinese", "lives in Hangzhou") where the hashing trick is
  competitive with real embeddings for nearest-neighbour recall.

If a deployment wants real semantic embeddings, set
``AGENTFORGE_VECTOR_BACKEND=chroma`` and provide a real
:class:`Embedder` in that backend (out of scope for 0.1.2).
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable, List, Protocol, runtime_checkable

# Pre-compiled tokeniser: lowercase, word chars only.  This intentionally
# ignores CJK characters; AgentForge is ASCII-first in 0.1.x and real
# multilingual support will be added when a real embedder lands.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str, max_tokens: int = 512) -> list[str]:
    """Tokenise ``text`` and truncate to ``max_tokens`` tokens."""
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())[:max_tokens]


@runtime_checkable
class Embedder(Protocol):
    """Anything that maps a string to a list of floats.

    The contract: ``len(embed(text))`` must be constant for the same
    instance and the returned list must contain only finite floats.
    """

    dim: int

    def __call__(self, text: str) -> list[float]:  # pragma: no cover - protocol
        ...


class HashingEmbedder:
    """Feature-hashing embedder producing unit-norm ``dim``-dim vectors.

    The mapping is deterministic: the same input always produces the
    same output, so embeddings are stable across processes.  That is
    what lets the in-process vector store be rebuilt from scratch on
    every restart and still score consistently.
    """

    def __init__(self, dim: int = 128) -> None:
        if dim < 1:
            raise ValueError(f"dim must be >= 1, got {dim}")
        self.dim = dim

    def __call__(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _tokens(text):
            # MD5 is overkill but it is in the stdlib and gives us 128
            # bits -- enough that hash collisions on small token sets
            # are vanishingly rare.  The first 8 bytes give us a
            # bucket index and a sign bit.
            digest = hashlib.md5(tok.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if (digest[4] & 1) else -1.0
            vec[bucket] += sign
        # L2-normalise so cosine == dot product.
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec


# ---- Blob codec -----------------------------------------------------------
#
# Vectors are persisted to SQLite via the ``Memory.embedding`` LargeBinary
# column.  We pack/unpack float32 little-endian for compact storage:
# 128 dims * 4 bytes = 512 bytes per row.

def pack_vector(vec: Iterable[float]) -> bytes:
    """Pack a list of floats into a compact float32 LE blob.

    Empty input produces an empty blob (``b""``) so that callers can
    distinguish "no embedding" (None) from "zero-length embedding"
    (rare but legal).
    """
    import struct

    data = list(vec)
    if not data:
        return b""
    return struct.pack(f"<{len(data)}f", *data)


def unpack_vector(blob: bytes | None) -> list[float]:
    """Inverse of :func:`pack_vector`.  Returns ``[]`` for ``None``."""
    import struct

    if not blob:
        return []
    n = len(blob) // 4
    if n == 0:
        return []
    return list(struct.unpack(f"<{n}f", blob))


# ---- Module-level helpers used by MemoryStore -----------------------------


_default_embedder: HashingEmbedder | None = None


def get_default_embedder(dim: int = 128) -> HashingEmbedder:
    """Lazy module-level singleton so callers do not pay construction cost
    on every call.  Tests can call :func:`_reset_default_embedder` to
    force a rebuild when the dim changes.
    """
    global _default_embedder
    if _default_embedder is None or _default_embedder.dim != dim:
        _default_embedder = HashingEmbedder(dim=dim)
    return _default_embedder


def _reset_default_embedder() -> None:
    """Test hook -- drop the cached embedder so a new dim is picked up."""
    global _default_embedder
    _default_embedder = None


def encode(
    text: str,
    *,
    dim: int = 128,
    max_tokens: int = 512,
) -> list[float]:
    """One-shot encode: tokenise, hash and normalise.

    Convenience for callers that do not want to manage a singleton.
    """
    tokens = _tokens(text, max_tokens=max_tokens)
    if not tokens:
        return [0.0] * dim
    return get_default_embedder(dim=dim)(text)


def decode(blob: bytes | None) -> list[float]:
    """Unpack a stored blob.  Thin wrapper kept here so callers do not
    have to import :func:`unpack_vector` directly.
    """
    return unpack_vector(blob)


__all__ = [
    "Embedder",
    "HashingEmbedder",
    "encode",
    "decode",
    "pack_vector",
    "unpack_vector",
    "get_default_embedder",
    "_reset_default_embedder",
    "_tokens",
]

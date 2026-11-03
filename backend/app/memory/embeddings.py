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
from typing import Any, Iterable, List, Protocol, runtime_checkable

# ASCII words plus CJK sequences.  CJK sequences are expanded into
# unigrams and bigrams below so the dependency-free fallback never turns
# Chinese-only text into a zero vector.
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]+$")


def _tokens(text: str, max_tokens: int = 512) -> list[str]:
    """Tokenise ``text`` and truncate to ``max_tokens`` tokens."""
    if not text:
        return []
    tokens: list[str] = []
    for token in _TOKEN_RE.findall(text.lower()):
        if _CJK_RE.match(token):
            tokens.extend(token)
            tokens.extend(token[index : index + 2] for index in range(len(token) - 1))
        else:
            tokens.append(token)
    return tokens[:max_tokens]


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


class SentenceTransformerEmbedder:
    """Lazy adapter for a real local multilingual sentence embedding model."""

    def __init__(self, model_name: str, *, local_files_only: bool = True) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "sentence-transformers is required for "
                "AGENTFORGE_EMBEDDING_PROVIDER=sentence_transformers"
            ) from exc
        self._model = SentenceTransformer(model_name, local_files_only=local_files_only)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def __call__(self, text: str) -> list[float]:
        if not text:
            return [0.0] * self.dim
        values = self._model.encode(text, normalize_embeddings=True)
        vector = [float(value) for value in (values.tolist() if hasattr(values, "tolist") else values)]
        if len(vector) != self.dim or not all(math.isfinite(value) for value in vector):
            raise RuntimeError("sentence-transformers returned an invalid embedding")
        return vector


# ---- Module-level helpers used by MemoryStore -----------------------------


_default_embedder: Embedder | None = None
_default_embedder_signature: tuple[Any, ...] | None = None


def get_default_embedder(dim: int = 128) -> Embedder:
    """Return the configured embedder, constructing heavy models only once."""
    global _default_embedder, _default_embedder_signature
    from app.config import get_settings

    settings = get_settings()
    provider = settings.embedding_provider
    signature: tuple[Any, ...] = (
        provider,
        dim if provider == "hashing" else settings.embedding_model,
        settings.embedding_local_files_only,
    )
    if _default_embedder is None or signature != _default_embedder_signature:
        if provider == "sentence_transformers":
            _default_embedder = SentenceTransformerEmbedder(
                settings.embedding_model,
                local_files_only=settings.embedding_local_files_only,
            )
        else:
            _default_embedder = HashingEmbedder(dim=dim)
        _default_embedder_signature = signature
    return _default_embedder


def _reset_default_embedder() -> None:
    """Test hook -- drop the cached embedder so a new config is picked up."""
    global _default_embedder, _default_embedder_signature
    _default_embedder = None
    _default_embedder_signature = None


def encode(
    text: str,
    *,
    dim: int = 128,
    max_tokens: int = 512,
) -> list[float]:
    """One-shot encode: tokenise, hash and normalise.

    Convenience for callers that do not want to manage a singleton.
    """
    embedder = get_default_embedder(dim=dim)
    tokens = _tokens(text, max_tokens=max_tokens)
    if not tokens:
        return [0.0] * embedder.dim
    return embedder(text)


def decode(blob: bytes | None) -> list[float]:
    """Unpack a stored blob.  Thin wrapper kept here so callers do not
    have to import :func:`unpack_vector` directly.
    """
    return unpack_vector(blob)


__all__ = [
    "Embedder",
    "HashingEmbedder",
    "SentenceTransformerEmbedder",
    "encode",
    "decode",
    "pack_vector",
    "unpack_vector",
    "get_default_embedder",
    "_reset_default_embedder",
    "_tokens",
]

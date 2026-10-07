"""Tests for the vector-memory subsystem (0.1.2).

These tests cover the building blocks added in 0.1.2:

* :mod:`app.memory.embeddings` -- hashing embedder + blob codec.
* :mod:`app.memory.vector_store` -- in-process nearest-neighbour
  backend.

The :class:`app.agent.memory.MemoryStore` integration is wired in
0.1.2's next slice; this file only asserts the infrastructure
pieces in isolation so the integration tests can focus on the
real-world path.
"""
from __future__ import annotations

import math
import threading

import pytest

from app.memory import (
    HashingEmbedder,
    InMemoryVectorStore,
    VectorHit,
    VectorStore,
    encode,
    get_vector_store,
    pack_vector,
    unpack_vector,
    _reset_default_embedder,
    _reset_default_vector_store,
)


# ---- Embeddings ----------------------------------------------------------


class TestHashingEmbedder:
    def test_returns_unit_norm_vector(self) -> None:
        emb = HashingEmbedder(dim=16)
        v = emb("the quick brown fox jumps over the lazy dog")
        assert len(v) == 16
        norm = math.sqrt(sum(x * x for x in v))
        assert math.isclose(norm, 1.0, abs_tol=1e-6)

    def test_is_deterministic(self) -> None:
        emb = HashingEmbedder(dim=32)
        a = emb("agentforge")
        b = emb("agentforge")
        assert a == b

    def test_similar_inputs_score_higher_than_unrelated(self) -> None:
        emb = HashingEmbedder(dim=64)
        anchor = emb("python programming language")
        related = emb("python language and programming")
        unrelated = emb("banana smoothie breakfast")
        cos_related = sum(a * b for a, b in zip(anchor, related))
        cos_unrelated = sum(a * b for a, b in zip(anchor, unrelated))
        assert cos_related > cos_unrelated

    def test_empty_input_returns_zero_vector(self) -> None:
        emb = HashingEmbedder(dim=8)
        assert emb("") == [0.0] * 8
        assert emb("   ??? !!!") == [0.0] * 8

    def test_dim_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            HashingEmbedder(dim=0)

    def test_module_level_encode(self) -> None:
        _reset_default_embedder()
        v = encode("hello world", dim=12)
        assert len(v) == 12
        # encode of empty returns the dim-length zero vector.
        assert encode("", dim=12) == [0.0] * 12


class TestBlobCodec:
    def test_roundtrip_preserves_values(self) -> None:
        v = [0.1, -0.2, 0.3, 0.4]
        blob = pack_vector(v)
        assert unpack_vector(blob) == pytest.approx(v, abs=1e-6)

    def test_empty_input_roundtrips(self) -> None:
        assert pack_vector([]) == b""
        assert unpack_vector(b"") == []

    def test_none_unpacks_to_empty(self) -> None:
        assert unpack_vector(None) == []

    def test_blob_size_scales_linearly(self) -> None:
        for dim in (1, 8, 128, 1024):
            v = [0.5] * dim
            blob = pack_vector(v)
            assert len(blob) == dim * 4  # float32

    def test_truncated_blob_does_not_crash(self) -> None:
        # 3 bytes is not a multiple of 4 -> should yield zero rows.
        assert unpack_vector(b"\x00\x00\x00") == []

# ---- Vector store --------------------------------------------------------


class TestInMemoryVectorStore:
    def test_upsert_and_search_returns_self_with_score_one(self) -> None:
        store = InMemoryVectorStore()
        store.upsert(["x"], [[1.0, 0.0, 0.0]])
        hits = store.search([1.0, 0.0, 0.0], k=1)
        assert len(hits) == 1
        assert hits[0].id == "x"
        assert math.isclose(hits[0].score, 1.0, abs_tol=1e-6)

    def test_search_ranks_closer_vectors_first(self) -> None:
        # Use a high enough dim that the hashing trick has room to
        # separate unrelated inputs; with dim=4 every short token
        # hashes to the same bucket and the signal is washed out.
        from app.memory import encode as _enc

        store = InMemoryVectorStore()
        store.upsert(
            ["perfect", "close"],
            [
                _enc("agent", dim=128),
                _enc("agent memory", dim=128),
            ],
        )
        hits = store.search(_enc("agent", dim=128), k=2)
        # ``perfect`` shares all of the query's tokens, ``close`` only
        # some, so ``perfect`` must come first and out-score ``close``.
        assert [h.id for h in hits] == ["perfect", "close"]
        assert hits[0].score > hits[1].score
        # Unrelated text should be filtered out (cosine ~ 0 / negative).
        store.upsert(["anti"], [_enc("completely unrelated recipe", dim=128)])
        hits = store.search(_enc("agent", dim=128), k=3)
        assert "anti" not in {h.id for h in hits}

    def test_delete_removes_only_listed_ids(self) -> None:
        store = InMemoryVectorStore()
        store.upsert(["a", "b", "c"], [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
        store.delete(["a"])
        assert len(store) == 2
        ids = {h.id for h in store.search([1.0, 0.0], k=5)}
        assert ids == {"c"}

    def test_upsert_replaces_existing_vector(self) -> None:
        store = InMemoryVectorStore()
        store.upsert(["a"], [[0.0, 1.0]])
        store.upsert(["a"], [[1.0, 0.0]])
        hits = store.search([1.0, 0.0], k=1)
        assert hits[0].id == "a"
        assert math.isclose(hits[0].score, 1.0, abs_tol=1e-6)

    def test_upsert_length_mismatch_raises(self) -> None:
        store = InMemoryVectorStore()
        with pytest.raises(ValueError):
            store.upsert(["a", "b"], [[1.0]])

    def test_search_with_empty_query_returns_empty(self) -> None:
        store = InMemoryVectorStore()
        store.upsert(["a"], [[1.0, 0.0]])
        assert store.search([], k=3) == []
        assert store.search([0.0, 0.0], k=3) == []

    def test_clear_drops_everything(self) -> None:
        store = InMemoryVectorStore()
        store.upsert(["a", "b"], [[1.0, 0.0], [0.0, 1.0]])
        store.clear()
        assert len(store) == 0

    def test_is_thread_safe(self) -> None:
        store = InMemoryVectorStore()
        errors: list[BaseException] = []

        def writer(prefix: str) -> None:
            try:
                for i in range(50):
                    store.upsert(
                        [f"{prefix}-{i}"], [[1.0 if i % 2 else -1.0, 0.0]]
                    )
            except BaseException as e:  # pragma: no cover - smoke
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(f"t{i}",)) for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        # 4 threads * 50 ids = 200 distinct keys.
        assert len(store) == 200


class TestVectorStoreProtocol:
    def test_in_memory_satisfies_protocol(self) -> None:
        assert isinstance(InMemoryVectorStore(), VectorStore)

    def test_get_vector_store_returns_inprocess_by_default(self) -> None:
        _reset_default_vector_store()
        store = get_vector_store()
        assert isinstance(store, InMemoryVectorStore)
        assert isinstance(store, VectorStore)

    def test_get_vector_store_is_singleton(self) -> None:
        _reset_default_vector_store()
        a = get_vector_store()
        b = get_vector_store()
        assert a is b

    def test_unknown_backend_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _reset_default_vector_store()
        # ``Settings`` uses ``env_prefix="AGENTFORGE_"`` so the env var
        # is ``AGENTFORGE_VECTOR_BACKEND`` (see ``app/config.py``).
        monkeypatch.setenv("AGENTFORGE_VECTOR_BACKEND", "weaviate")
        # Clear the cached settings so the new env var is read.
        from app.config import get_settings as _get_settings
        _get_settings.cache_clear()  # type: ignore[attr-defined]
        try:
            with pytest.raises(ValueError, match="Unknown vector backend"):
                get_vector_store()
        finally:
            _reset_default_vector_store()
            _get_settings.cache_clear()  # type: ignore[attr-defined]


# ---- End-to-end smoke ----------------------------------------------------


def test_end_to_end_text_similarity_pipeline() -> None:
    """Encode -> upsert -> search should rank semantically similar docs first."""
    _reset_default_embedder()
    _reset_default_vector_store()
    store = get_vector_store()
    docs = {
        "m1": "The user prefers concise answers in Chinese.",
        "m2": "The user likes short Chinese replies.",
        "m3": "Today's weather is sunny with a high of 25C.",
    }
    store.upsert(
        list(docs.keys()), [encode(text, dim=64) for text in docs.values()]
    )

    hits = store.search(
        encode("user prefers short chinese answers", dim=64), k=3
    )
    assert len(hits) == 3
    # m1 and m2 share many tokens with the query; m3 (weather) should rank last.
    assert hits[-1].id == "m3"
    assert hits[0].score >= hits[-1].score


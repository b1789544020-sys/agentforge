"""Integration tests for :class:`app.agent.memory.MemoryStore`.

These tests exercise the wiring between the ORM, the hashing-trick
embedder and the in-process vector store added in 0.1.2 4.2.

They are deliberately kept separate from the unit tests in
``test_memory_vector.py`` so a regression in the embedder or the
vector store does not mask a wiring bug (or vice versa).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.memory import MemoryStore
from app.memory import (
    InMemoryVectorStore,
    _reset_default_vector_store,
    _reset_default_embedder,
)
from app.models import Agent


# ---- Fixtures -------------------------------------------------------------


@pytest_asyncio.fixture
async def agent_id(db_session: AsyncSession) -> str:
    """Insert a throwaway agent and return its id."""
    a = Agent(name="memory_integration", system_prompt="x", model="mock")
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    return a.id


@pytest.fixture(autouse=True)
def _isolate_vector_state() -> None:
    """Each test gets a clean vector index + embedder cache.

    We reset both because ``InMemoryVectorStore`` is a process-wide
    singleton; without this isolation the order of test execution
    would affect what ``search`` returns.
    """
    _reset_default_vector_store()
    _reset_default_embedder()
    yield
    _reset_default_vector_store()

# ---- Tests ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_persists_embedding_blob(
    db_session: AsyncSession, agent_id: str
) -> None:
    store = _store(db_session, agent_id)
    item = await store.add("hello world")
    assert item.id
    from app.models import Memory

    row = await db_session.get(Memory, item.id)
    assert row is not None
    assert row.embedding is not None
    assert len(row.embedding) > 0  # 4 * dim bytes
    # ``session_id`` column should accept the new keyword too.
    item2 = await store.add("with session", session_id="s-1")
    row2 = await db_session.get(Memory, item2.id)
    assert row2 is not None
    assert row2.session_id == "s-1"


@pytest.mark.asyncio
async def test_search_embed_ranks_semantically_similar_first(
    db_session: AsyncSession, agent_id: str
) -> None:
    store = _store(db_session, agent_id)
    await store.add("user prefers concise answers in chinese")
    await store.add("user likes short replies in chinese")
    await store.add("today the weather is sunny with 25 degrees")

    hits = await store.search(
        "user wants short chinese answers", limit=3, mode="embed"
    )
    assert len(hits) == 2
    assert "today the weather" not in {h[0].content for h in hits}
    assert hits[0][1] >= hits[1][1]


@pytest.mark.asyncio
async def test_search_hybrid_unifies_exact_and_embed(
    db_session: AsyncSession, agent_id: str
) -> None:
    store = _store(db_session, agent_id)
    await store.add("python is a programming language")
    await store.add("i love eating python bananas for breakfast")
    await store.add("java is also a programming language")
    hits = await store.search("java", limit=5, mode="hybrid")
    contents = [h[0].content for h in hits]
    assert "java is also a programming language" in contents
    assert "python is a programming language" in contents


@pytest.mark.asyncio
async def test_delete_removes_from_vector_index(
    db_session: AsyncSession, agent_id: str
) -> None:
    store = _store(db_session, agent_id)
    item = await store.add("a very specific memory about python snakes")
    hits = await store.search("python snakes", limit=5, mode="embed")
    assert any(h[0].id == item.id for h in hits)
    assert await store.delete(item.id) is True
    hits = await store.search("python snakes", limit=5, mode="embed")
    assert all(h[0].id != item.id for h in hits)


@pytest.mark.asyncio
async def test_explicit_embedding_kwarg_skips_automatic_encode(
    db_session: AsyncSession, agent_id: str
) -> None:
    """Passing ``embedding=`` should be honoured verbatim."""
    store = _store(db_session, agent_id)
    fake = [0.0] * 128
    fake[0] = 1.0
    item = await store.add("ignored", embedding=fake)
    from app.models import Memory
    from app.memory.embeddings import unpack_vector

    row = await db_session.get(Memory, item.id)
    assert row is not None
    assert unpack_vector(row.embedding) == pytest.approx(fake, abs=1e-6)


@pytest.mark.asyncio
async def test_search_with_empty_query_embedding_returns_empty(
    db_session: AsyncSession, agent_id: str
) -> None:
    store = _store(db_session, agent_id)
    await store.add("anything at all here")
    hits = await store.search("", limit=5, mode="embed")
    assert hits == []


@pytest.mark.asyncio
async def test_vector_index_mirrors_writes(
    db_session: AsyncSession, agent_id: str
) -> None:
    """The in-process vector store should contain exactly the ids we wrote."""
    from app.memory.vector_store import get_vector_store
    from app.memory.embeddings import encode as _enc

    store = _store(db_session, agent_id)
    a = await store.add("alpha unique token xyz")
    b = await store.add("beta unique token abc")
    c = await store.add("gamma unique token def")
    idx = get_vector_store()
    assert isinstance(idx, InMemoryVectorStore)
    # Probe with one of the stored contents so cosine > 0 for at least
    # the corresponding row.
    hits = idx.search(_enc("alpha unique token xyz", dim=128), k=10)
    assert a.id in {h.id for h in hits}
    assert b.id in {h.id for h in hits}
    assert c.id in {h.id for h in hits}

    _reset_default_embedder()


def _store(session: AsyncSession, agent_id: str) -> MemoryStore:
    return MemoryStore(session, agent_id=agent_id)

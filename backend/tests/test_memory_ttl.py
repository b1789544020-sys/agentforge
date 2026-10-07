"""Integration tests for memory TTL + cleanup (0.1.2 4.3)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.memory import MemoryStore, cleanup_expired
from app.memory import (
    InMemoryVectorStore,
    _reset_default_vector_store,
    schedule_periodic_cleanup,
)
from app.models import Agent, Memory


# ---- Fixtures -------------------------------------------------------------


@pytest_asyncio.fixture
async def agent_id(db_session: AsyncSession) -> str:
    a = Agent(name="ttl_integration", system_prompt="x", model="mock")
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    return a.id


@pytest.fixture(autouse=True)
def _isolate_vector_state() -> None:
    _reset_default_vector_store()
    yield
    _reset_default_vector_store()


def _store(session: AsyncSession, agent_id: str) -> MemoryStore:
    return MemoryStore(session, agent_id=agent_id)


# ---- Tests ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_with_ttl_populates_expires_at(
    db_session: AsyncSession, agent_id: str
) -> None:
    store = _store(db_session, agent_id)
    item = await store.add("expires soon", ttl_seconds=120)
    row = await db_session.get(Memory, item.id)
    assert row is not None
    assert row.expires_at is not None
    delta = (row.expires_at - datetime.utcnow()).total_seconds()
    # Should be ~120s in the future, allow generous clock drift.
    assert 100 < delta < 140
    # No-TTL row stays at NULL.
    plain = await store.add("permanent")
    assert plain.expires_at is None


@pytest.mark.asyncio
async def test_list_and_search_hide_expired_rows(
    db_session: AsyncSession, agent_id: str
) -> None:
    store = _store(db_session, agent_id)
    keep = await store.add("user likes short chinese answers")
    expire = await store.add("user likes short chinese answers", ttl_seconds=10)
    # Force the second row to be already expired.
    await db_session.execute(
        update(Memory)
        .where(Memory.id == expire.id)
        .values(expires_at=datetime.utcnow() - timedelta(seconds=1))
    )
    await db_session.commit()

    listed = await store.list(limit=10)
    assert {it.id for it in listed} == {keep.id}

    # Embed search must also skip the expired row, even if the vector
    # index has not been cleaned up yet.
    hits = await store.search("short chinese answers", limit=5, mode="embed")
    assert {h[0].id for h in hits} == {keep.id}
    # And the same for exact / hybrid.
    hits = await store.search("short chinese answers", limit=5, mode="exact")
    assert {h[0].id for h in hits} == {keep.id}
    hits = await store.search("short chinese answers", limit=5, mode="hybrid")
    assert {h[0].id for h in hits} == {keep.id}


@pytest.mark.asyncio
async def test_cleanup_expired_deletes_and_returns_count(
    db_session: AsyncSession, agent_id: str
) -> None:
    store = _store(db_session, agent_id)
    live = await store.add("live", ttl_seconds=3600)
    dead_a = await store.add("dead a", ttl_seconds=10)
    dead_b = await store.add("dead b", ttl_seconds=10)
    # Expire dead_* in one UPDATE.
    await db_session.execute(
        update(Memory)
        .where(Memory.id.in_([dead_a.id, dead_b.id]))
        .values(expires_at=datetime.utcnow() - timedelta(seconds=1))
    )
    await db_session.commit()

    deleted = await cleanup_expired(db_session, agent_id=agent_id)
    assert deleted == 2

    # Live row still there; the two dead ones are gone from both DB
    # and the in-process vector index.
    assert await db_session.get(Memory, live.id) is not None
    assert await db_session.get(Memory, dead_a.id) is None
    assert await db_session.get(Memory, dead_b.id) is None
    from app.memory.vector_store import get_vector_store

    idx = get_vector_store()
    assert isinstance(idx, InMemoryVectorStore)
    indexed = {h.id for h in idx.search([0.0] + [0.0] * 127, k=20)}
    # The dead ones must not be findable.
    assert dead_a.id not in indexed
    assert dead_b.id not in indexed


@pytest.mark.asyncio
async def test_periodic_cleanup_runs_and_can_be_cancelled(
    db_session: AsyncSession, agent_id: str
) -> None:
    """``schedule_periodic_cleanup`` should keep running until cancelled
    and tolerate at least one full cycle (including removing a row)."""
    store = _store(db_session, agent_id)
    expired = await store.add("will be cleaned", ttl_seconds=10)
    # Force expiry.
    await db_session.execute(
        update(Memory)
        .where(Memory.id == expired.id)
        .values(expires_at=datetime.utcnow() - timedelta(seconds=1))
    )
    await db_session.commit()

    # Use the conftest ``db_session`` directly as the session -- it is
    # bound to the in-memory engine so the periodic loop sees the row
    # we just inserted.  Cleanup only reads/deletes so sharing the
    # session is safe for the test.
    task = schedule_periodic_cleanup(
        lambda: db_session, interval_seconds=0.05
    )
    # Give the loop a couple of cycles to run cleanup.
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # The expired row is gone.
    assert await db_session.get(Memory, expired.id) is None

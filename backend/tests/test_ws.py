"""Tests for the WebSocket live-trace broadcaster and endpoint (0.1.2 #10).

Covers:

* :class:`RunBroadcaster` — subscribe / publish / unsubscribe semantics,
  slow-consumer pruning, and channel cleanup.
* :func:`app.api.ws.stream_run` — happy-path snapshot delivery, terminal
  short-circuit (already-completed run), and the 404 close path.
* Integration: a full ``POST /runs`` then ``ws /runs/{id}/ws`` flow that
  asserts the wire shape matches the protocol documented in
  :mod:`app.api.ws`.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.ws_broadcaster import RunBroadcaster
from app.main import app


@pytest.fixture
def fresh_broadcaster() -> Iterator[RunBroadcaster]:
    """Yield a clean :class:`RunBroadcaster` for each test."""
    yield RunBroadcaster()


async def test_subscribe_and_publish(fresh_broadcaster: RunBroadcaster) -> None:
    q = await fresh_broadcaster.subscribe("r1")
    fresh_broadcaster.publish_step("r1", {"id": "s1", "kind": "think"})
    msg = await asyncio.wait_for(q.get(), timeout=1.0)
    assert msg == {"event": "run.step", "id": "s1", "kind": "think"}


async def test_unsubscribe_is_idempotent(fresh_broadcaster: RunBroadcaster) -> None:
    q = await fresh_broadcaster.subscribe("r1")
    await fresh_broadcaster.unsubscribe("r1", q)
    await fresh_broadcaster.unsubscribe("r1", q)  # second call: no error
    assert fresh_broadcaster.subscriber_count("r1") == 0
    fresh_broadcaster.publish_step("r1", {"id": "stale"})
    assert q.empty()


async def test_channel_pruned_when_last_subscriber_leaves(
    fresh_broadcaster: RunBroadcaster,
) -> None:
    q = await fresh_broadcaster.subscribe("r1")
    await fresh_broadcaster.unsubscribe("r1", q)
    assert "r1" not in list(fresh_broadcaster.active_runs())


async def test_slow_consumer_is_dropped(fresh_broadcaster: RunBroadcaster) -> None:
    """Flood a 1-slot subscriber; the broadcaster prunes and sends a sentinel."""
    q = await fresh_broadcaster.subscribe("r1", maxsize=1)
    # First publish fills the queue; the second must trip the
    # full-queue branch, marking us dead and putting a sentinel
    # into the now-vacated slot.
    fresh_broadcaster.publish_step("r1", {"id": "first"})
    fresh_broadcaster.publish_step("r1", {"id": "second"})
    saw_drop = False
    while not q.empty():
        item = q.get_nowait()
        if item.get("event") == "__dropped__":
            saw_drop = True
            break
    assert saw_drop, "expected __dropped__ sentinel for slow consumer"
    assert fresh_broadcaster.subscriber_count("r1") == 0


# ---------------------------------------------------------------------------
# WebSocket endpoint tests (via FastAPI TestClient)
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _create_agent(client: TestClient) -> dict:
    r = client.post(
        "/api/agents",
        json={
            "name": f"ws-test-{uuid.uuid4().hex[:8]}",
            "system_prompt": "Test",
            "model": "mock",
            "tools": [],
        },
    )
    # The /api/agents endpoint returns 201 (Created) for new agents.
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_ws_unknown_run_closes(client: TestClient) -> None:
    """A bad run_id is rejected with 4404 before the handshake completes."""
    fake_agent = uuid.uuid4().hex
    fake_run = uuid.uuid4().hex
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/api/agents/{fake_agent}/runs/{fake_run}/ws"
        ) as ws:
            ws.receive_text()


def test_ws_terminal_run_short_circuits(client: TestClient) -> None:
    """Connecting to a finished run yields snapshot + run.complete only."""
    agent = _create_agent(client)
    run = client.post(
        f"/api/agents/{agent['id']}/runs",
        json={"input": "hello", "async_run": True},
    ).json()
    run_id = run["id"]
    deadline = time.time() + 5.0
    while time.time() < deadline:
        detail = client.get(f"/api/agents/{agent['id']}/runs/{run_id}").json()
        if detail["status"] in {"success", "failed", "cancelled", "terminated"}:
            break
        time.sleep(0.1)
    else:
        pytest.fail("run did not finish within 5s")

    with client.websocket_connect(
        f"/api/agents/{agent['id']}/runs/{run_id}/ws"
    ) as ws:
        snap = json.loads(ws.receive_text())
        complete = json.loads(ws.receive_text())
    assert snap["event"] == "run.snapshot"
    assert snap["run"]["id"] == run_id
    assert len(snap["run"]["trace"]) >= 1
    assert complete["event"] == "run.complete"
    assert complete["status"] == detail["status"]


def test_ws_live_run_streams_steps(client: TestClient) -> None:
    """An in-flight run pushes snapshot+steps+complete in order."""
    agent = _create_agent(client)
    # Warm the agent's on_step machinery with a sync run.
    _ = client.post(
        f"/api/agents/{agent['id']}/runs",
        json={"input": "warmup", "async_run": False},
    ).json()
    run = client.post(
        f"/api/agents/{agent['id']}/runs",
        json={"input": "stream me", "async_run": True},
    ).json()
    run_id = run["id"]
    with client.websocket_connect(
        f"/api/agents/{agent['id']}/runs/{run_id}/ws"
    ) as ws:
        events: list[dict] = []
        while True:
            msg = json.loads(ws.receive_text())
            events.append(msg)
            if msg["event"] == "run.complete":
                break
    kinds = [m["event"] for m in events]
    assert kinds[0] == "run.snapshot", kinds
    assert kinds[-1] == "run.complete", kinds
    snap_trace_len = len(events[0]["run"].get("trace") or [])
    step_frames = [m for m in events if m["event"] == "run.step"]
    assert snap_trace_len + len(step_frames) >= 1

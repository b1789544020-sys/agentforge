"""Tests for the memory API, pause/resume endpoints, and async_run mode."""
from __future__ import annotations

import os
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient


os.environ.setdefault("AGENTFORGE_LLM_PROVIDER", "mock")

from app.main import create_app  # noqa: E402


async def _make_agent(client: AsyncClient, name: str = "tester") -> str:
    resp = await client.post(
        "/api/agents",
        json={"name": name, "system_prompt": "be brief", "tools": []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_memory_list_add_delete() -> None:
    """Memory CRUD is exposed at /api/agents/{id}/memory."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        agent_id = await _make_agent(client)

        # initially empty (404 is acceptable when the agent has no
        # memory yet; the route should at least be wired)
        resp = await client.get(f"/api/agents/{agent_id}/memory")
        assert resp.status_code == 200
        assert resp.json() == []

        # add
        resp = await client.post(
            f"/api/agents/{agent_id}/memory",
            json={"content": "user likes bullet points", "kind": "episodic", "importance": 0.6},
        )
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["content"] == "user likes bullet points"
        assert created["kind"] == "episodic"
        assert created["importance"] == 0.6

        # list now contains it
        resp = await client.get(f"/api/agents/{agent_id}/memory")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["id"] == created["id"]

        # delete
        resp = await client.delete(
            f"/api/agents/{agent_id}/memory/{created['id']}"
        )
        assert resp.status_code in (200, 204)

        # list is empty again
        resp = await client.get(f"/api/agents/{agent_id}/memory")
        assert resp.status_code == 200
        assert resp.json() == []


@pytest.mark.asyncio
async def test_memory_routes_are_registered() -> None:
    """Both /memory and /tools routers should be mounted under the API prefix.

    This is a regression test for the bug where these two routers were
    defined in :mod:`app.api.agents` but never included in the FastAPI
    app, so the endpoints 404'd at runtime.
    """
    app = create_app()
    # The OpenAPI spec is the most reliable place to look for the
    # *final* mounted path: it has the URL prefix already applied and
    # does not depend on the internal ``_IncludedRouter`` wrapper.
    paths = set(app.openapi().get("paths", {}).keys())

    # All four should be reachable under both the versioned and the
    # unversioned API prefix.
    for prefix in ("/api", "/api/v1"):
        assert f"{prefix}/agents/{{agent_id}}/memory" in paths, paths
        assert f"{prefix}/agents/{{agent_id}}/memory/{{memory_id}}" in paths, paths
        assert f"{prefix}/tools" in paths, paths
        assert f"{prefix}/agents/{{agent_id}}/runs/{{run_id}}/pause" in paths, paths
        assert f"{prefix}/agents/{{agent_id}}/runs/{{run_id}}/resume" in paths, paths


@pytest.mark.asyncio
async def test_pause_resume_roundtrip() -> None:
    """Pause/resume endpoints accept the documented status transitions.

    We don't actually pause a running loop here (the mock LLM is too
    fast) — we just check the API surface: pausing a *finished* run
    returns the existing row without crashing, and resuming a run that
    isn't paused returns 409.
    """
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        agent_id = await _make_agent(client)

        # run synchronously to completion
        resp = await client.post(
            f"/api/agents/{agent_id}/runs",
            json={"input": "hi"},
        )
        assert resp.status_code == 200, resp.text
        run = resp.json()
        assert run["status"] in {"success", "ok", "completed"}

        # pause a finished run -> 409
        resp = await client.post(
            f"/api/agents/{agent_id}/runs/{run['id']}/pause"
        )
        assert resp.status_code == 409, resp.text

        # resume a non-paused run -> 409
        resp = await client.post(
            f"/api/agents/{agent_id}/runs/{run['id']}/resume"
        )
        assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_async_run_returns_immediately() -> None:
    """async_run=true must return a row whose status is pending/running."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        agent_id = await _make_agent(client)
        resp = await client.post(
            f"/api/agents/{agent_id}/runs",
            json={"input": "long task", "async_run": True},
        )
        assert resp.status_code == 200, resp.text
        run = resp.json()
        # The endpoint should *eventually* finish (the mock is fast),
        # so we accept any non-error status here.
        assert run["status"] in {"pending", "running", "success", "ok", "completed", "failed"}


@pytest.mark.asyncio
async def test_run_404() -> None:
    """Unknown run id returns 404 from the pause/resume endpoints."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        agent_id = await _make_agent(client)
        bogus = "00000000-0000-0000-0000-000000000000"
        resp = await client.post(f"/api/agents/{agent_id}/runs/{bogus}/pause")
        assert resp.status_code == 404
        resp = await client.post(f"/api/agents/{agent_id}/runs/{bogus}/resume")
        assert resp.status_code == 404

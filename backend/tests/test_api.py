"""Smoke tests for the public API (using the in-process mock LLM)."""
from __future__ import annotations

import os
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient


os.environ.setdefault("AGENTFORGE_LLM_PROVIDER", "mock")

from app.main import create_app  # noqa: E402


@pytest.mark.asyncio
async def test_health() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_agent_lifecycle() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # create
        resp = await client.post(
            "/api/agents",
            json={"name": "tester", "system_prompt": "You are helpful.", "tools": ["echo"]},
        )
        assert resp.status_code == 201, resp.text
        agent = resp.json()
        agent_id = agent["id"]

        # list
        resp = await client.get("/api/agents")
        assert resp.status_code == 200
        assert any(a["id"] == agent_id for a in resp.json())

        # run
        resp = await client.post(
            f"/api/agents/{agent_id}/runs",
            json={"input": "hello"},
        )
        assert resp.status_code == 200, resp.text
        run = resp.json()
        assert run["status"] in {"success", "ok", "completed"}
        assert run["step_count"] >= 1

        # get run with trace
        resp = await client.get(f"/api/agents/{agent_id}/runs/{run['id']}")
        assert resp.status_code == 200
        detail = resp.json()
        assert "trace" in detail
        assert len(detail["trace"]) >= 1

        # delete
        resp = await client.delete(f"/api/agents/{agent_id}")
        assert resp.status_code == 204


@pytest.mark.asyncio
async def test_eval_basic() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/agents",
            json={"name": "ev", "tools": []},
        )
        agent_id = resp.json()["id"]

        resp = await client.post(
            f"/api/agents/{agent_id}/evals",
            json={
                "dataset_name": "smoke",
                "scorer": "contains",
                "pass_threshold": 0.5,
                "cases": [
                    {"input": "say hi", "expected": "hi"},
                    {"input": "say bye", "expected": "bye"},
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        eval_run = resp.json()
        assert eval_run["status"] in {"pending", "running", "completed", "failed"}
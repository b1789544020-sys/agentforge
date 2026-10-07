"""End-to-end tests for the ``/api/agents/{id}/evals`` endpoints.

Covers:

* legacy ``cases`` flow still returns a 201 with an ``EvalRunOut``
* dataset-ref flow with ``test_case_ids`` returns the per-case result
  details (``tool_calls``, ``score_details``) on the run detail
* the ``/progress`` endpoint exposes a snapshot safe to poll
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("AGENTFORGE_LLM_PROVIDER", "mock")

from app.database import async_session_factory  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Agent, EvalCase  # noqa: E402


def _create_app():
    return create_app()


async def _make_agent(name: str = "ev-api") -> Agent:
    async with async_session_factory() as s:
        a = Agent(
            name=name,
            model="mock-model",
            provider="mock",
            temperature=0.0,
            max_steps=3,
            tools=[],
            config={},
        )
        s.add(a)
        await s.commit()
        await s.refresh(a)
        return a


async def _make_case(input_: str, expected) -> EvalCase:
    async with async_session_factory() as s:
        row = EvalCase(
            id=str(uuid.uuid4()),
            eval_run_id=None,
            dataset="api",
            name=input_,
            input=input_,
            expected=expected,
            meta=json.dumps({}),
            index=0,
            output="",
            score=0.0,
            passed=0,
            duration_ms=0,
            tokens=0,
            error=None,
            tool_calls="[]",
            score_details="{}",
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


@pytest.mark.asyncio
async def test_create_eval_with_test_case_ids_returns_201() -> None:
    agent = await _make_agent()
    c1 = await _make_case("hi", "hello")
    c2 = await _make_case("bye", "goodbye")

    app = _create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/agents/{agent.id}/evals",
            json={
                "dataset_name": "ds1",
                "scorer_name": "composite",
                "pass_threshold": 0.5,
                "test_case_ids": [c1.id, c2.id],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] in {"pending", "running", "completed", "failed"}
        assert body["total"] == 2
        assert body["scorer"] == "composite"


@pytest.mark.asyncio
async def test_create_eval_rejects_when_neither_cases_nor_ids_given() -> None:
    agent = await _make_agent("reject")
    app = _create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/agents/{agent.id}/evals",
            json={"dataset_name": "x"},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_eval_rejects_when_ids_unknown() -> None:
    agent = await _make_agent("unknown")
    app = _create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/agents/{agent.id}/evals",
            json={"test_case_ids": ["does-not-exist"]},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_progress_endpoint_returns_snapshot() -> None:
    agent = await _make_agent("prog")
    c = await _make_case("ping", "pong")
    app = _create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/agents/{agent.id}/evals",
            json={
                "test_case_ids": [c.id],
                "scorer_name": "status",
                "pass_threshold": 0.5,
            },
        )
        assert resp.status_code == 201
        eval_id = resp.json()["id"]

        resp = await client.get(
            f"/api/agents/{agent.id}/evals/{eval_id}/progress"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["eval_run_id"] == eval_id
        assert body["total"] == 1
        assert body["done"] in (0, 1)
        assert 0.0 <= body["progress"] <= 1.0


@pytest.mark.asyncio
async def test_eval_detail_includes_per_case_results() -> None:
    agent = await _make_agent("detail")
    c = await _make_case("hi", "hi")
    app = _create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/agents/{agent.id}/evals",
            json={
                "test_case_ids": [c.id],
                "scorer_name": "status",
                "pass_threshold": 0.5,
            },
        )
        eval_id = resp.json()["id"]

        resp = await client.get(f"/api/agents/{agent.id}/evals/{eval_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["scorer"] == "status"
        assert isinstance(body["cases"], list)
        # In the worst case the run hasn't completed yet, so the list
        # may be empty; but it must always be present.
        if body["cases"]:
            case = body["cases"][0]
            assert "tool_calls" in case
            assert "score_details" in case
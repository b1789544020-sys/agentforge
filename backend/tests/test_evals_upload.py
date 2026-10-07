"""End-to-end tests for ``POST /api/agents/{id}/evals/upload`` (0.1.3 #2).

Covers the happy path for both ``.jsonl`` and ``.csv`` uploads, the
malformed-file 400 path, and the agent-not-found 404 path.  The
endpoint runs :class:`EvalRunner` in a background task, so these
tests only assert on the synchronous response (run id, format,
counts) and not on the eventual per-case scores.
"""
from __future__ import annotations

import io
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("AGENTFORGE_LLM_PROVIDER", "mock")

from app.database import async_session_factory  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Agent  # noqa: E402


def _create_app():
    return create_app()


async def _make_agent(name: str = "upload") -> Agent:
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


def _make_file(name: str, content: str) -> dict:
    """Build a ``files=`` payload for ``httpx`` multipart upload."""
    return {
        "file": (
            name,
            io.BytesIO(content.encode("utf-8")),
            "application/octet-stream",
        )
    }


@pytest.mark.asyncio
async def test_upload_jsonl_returns_201_and_run_id() -> None:
    agent = await _make_agent("up-jsonl")
    app = _create_app()
    jsonl = (
        '{"input": "What is 2+2?", "expected": "4"}\n'
        '{"input": "Capital of France?", "expected": "Paris"}\n'
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/agents/{agent.id}/evals/upload",
            files=_make_file("cases.jsonl", jsonl),
            data={"dataset_name": "demo-jsonl"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["run_id"]
        uuid.UUID(body["run_id"])  # parseable
        assert body["dataset_name"] == "demo-jsonl"
        assert body["case_count"] == 2
        assert body["accepted"] == 2
        assert body["rejected"] == 0
        assert body["format"] == "jsonl"


@pytest.mark.asyncio
async def test_upload_csv_returns_201_and_run_id() -> None:
    agent = await _make_agent("up-csv")
    app = _create_app()
    csv = (
        "input,expected,name,tags,source\n"
        "What is 2+2?,4,arithmetic,math;easy,seed1\n"
        "Capital of France?,Paris,geo,geography,seed2\n"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/agents/{agent.id}/evals/upload",
            files=_make_file("cases.csv", csv),
            data={"dataset_name": "demo-csv"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["run_id"]
        assert body["dataset_name"] == "demo-csv"
        assert body["case_count"] == 2
        assert body["format"] == "csv"


@pytest.mark.asyncio
async def test_upload_malformed_jsonl_returns_400_with_line_numbers() -> None:
    agent = await _make_agent("up-bad")
    app = _create_app()
    bad = (
        '{"expected": "no input"}\n'  # line 1: missing 'input'
        '{"input": 42}\n'  # line 2: input not a string
        '{"input": "ok"}\n'  # line 3: valid
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/agents/{agent.id}/evals/upload",
            files=_make_file("bad.jsonl", bad),
            data={"dataset_name": "should-fail"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        detail = body["detail"]
        assert "errors" in detail, detail
        lines = sorted({err["line"] for err in detail["errors"]})
        assert 1 in lines and 2 in lines
        messages = " ".join(err["message"] for err in detail["errors"])
        assert "input" in messages


@pytest.mark.asyncio
async def test_upload_malformed_csv_returns_400() -> None:
    agent = await _make_agent("up-bad-csv")
    app = _create_app()
    bad = "expected,name\n4,arithmetic\n"  # no 'input' column
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/agents/{agent.id}/evals/upload",
            files=_make_file("bad.csv", bad),
            data={"dataset_name": "x"},
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert "errors" in detail
        assert "input" in detail["errors"][0]["message"]


@pytest.mark.asyncio
async def test_upload_unknown_agent_returns_404() -> None:
    app = _create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/agents/does-not-exist/evals/upload",
            files=_make_file("cases.jsonl", '{"input": "q"}\n'),
            data={"dataset_name": "x"},
        )
        assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_upload_invalid_utf8_returns_400() -> None:
    agent = await _make_agent("up-utf8")
    app = _create_app()
    # raw bytes that are not valid utf-8
    payload = b'\xff\xfe{"input": "q"}\n'
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/agents/{agent.id}/evals/upload",
            files={
                "file": (
                    "cases.jsonl",
                    io.BytesIO(payload),
                    "application/octet-stream",
                )
            },
            data={"dataset_name": "x"},
        )
        assert resp.status_code == 400, resp.text
        assert "utf-8" in resp.text.lower()


@pytest.mark.asyncio
async def test_upload_legacy_route_mirror_under_api() -> None:
    """The same route is also mounted under ``/api/...`` (no /v1)."""
    agent = await _make_agent("up-legacy")
    app = _create_app()
    jsonl = '{"input": "hello"}\n'
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/agents/{agent.id}/evals/upload",
            files=_make_file("cases.jsonl", jsonl),
            data={"dataset_name": "legacy"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["format"] == "jsonl"


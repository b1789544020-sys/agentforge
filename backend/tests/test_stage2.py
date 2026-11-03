"""Stage 2 regression tests for unified eval, search, and prompt versions."""
from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent.exporter import agent_to_dict
from app.agent.loop import AgentLoop
from app.database import async_session_factory
from app.llm.client import ChatResult
from app.main import create_app
from app.models import Agent


async def _agent(client: AsyncClient, name: str = "stage2") -> str:
    response = await client.post(
        "/api/agents",
        json={"name": name, "provider": "mock", "model": "mock", "tools": []},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.asyncio
async def test_memory_api_text_semantic_and_hybrid_search() -> None:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        agent_id = await _agent(client, "memory-search")
        response = await client.post(
            f"/api/agents/{agent_id}/memory",
            json={"content": "用户最喜欢喝咖啡", "kind": "preference"},
        )
        assert response.status_code == 201, response.text
        await client.post(
            f"/api/agents/{agent_id}/memory",
            json={"content": "今天天气晴朗", "kind": "note"},
        )
        for mode in ("exact", "semantic", "hybrid"):
            response = await client.get(
                f"/api/agents/{agent_id}/memory",
                params={"q": "咖啡", "mode": mode, "top_k": 1},
            )
            assert response.status_code == 200, response.text
            rows = response.json()
            assert len(rows) == 1
            assert rows[0]["content"] == "用户最喜欢喝咖啡"
            if mode != "exact":
                assert rows[0]["score"] > 0


@pytest.mark.asyncio
async def test_jinja_system_prompt_and_agent_export(db_session) -> None:
    captured: list[dict[str, Any]] = []

    class RecordingLLM:
        async def chat(self, messages: list[dict[str, Any]], **_kwargs: Any) -> ChatResult:
            captured.extend(messages)
            return ChatResult(content="done")

    agent = Agent(
        name="jinja",
        provider="mock",
        model="mock",
        system_prompt="你好，{{ name }}，任务是 {{ input }}",
        config={"prompt_variables": {"name": "小明"}},
        tools=[],
    )
    db_session.add(agent)
    await db_session.commit()
    result = await AgentLoop(agent, db_session, llm=RecordingLLM()).run("写一首诗")
    assert result.status == "success"
    assert captured[0]["content"] == "你好，小明，任务是 写一首诗"
    exported = agent_to_dict(agent)
    assert exported["config"]["prompt_variables"] == {"name": "小明"}
    assert "{{ name }}" in exported["system_prompt"]


@pytest.mark.asyncio
async def test_existing_run_uses_eval_runner() -> None:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        agent_id = await _agent(client, "run-eval")
        response = await client.post(f"/api/agents/{agent_id}/runs", json={"input": "hello"})
        run_id = response.json()["id"]
        response = await client.post(
            f"/api/agents/{agent_id}/runs/{run_id}/evaluate",
            json={"expected": "Mock response (no upstream call).", "scorer_name": "answer"},
        )
        assert response.status_code == 201, response.text
        evaluation = response.json()
        assert evaluation["status"] in {"pending", "running", "completed"}
        response = await client.get(f"/api/agents/{agent_id}/evals/{evaluation['id']}")
        evaluation = response.json()
        assert evaluation["status"] == "completed"
        assert evaluation["total"] == 1
        assert evaluation["passed"] == 1


@pytest.mark.asyncio
async def test_prompt_template_versions_bind_and_compare() -> None:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        agent_id = await _agent(client, "prompt-versions")
        response = await client.post(
            f"/api/agents/{agent_id}/prompt-templates",
            json={"name": "writer", "template": "你是{{ role }}", "description": "v1"},
        )
        assert response.status_code == 201, response.text
        template = response.json()
        assert template["latest_version"] == 1
        template_id = template["id"]
        response = await client.post(
            f"/api/agents/{agent_id}/prompt-templates/{template_id}/versions",
            json={"template": "你是资深{{ role }}"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["version"] == 2
        response = await client.post(
            f"/api/agents/{agent_id}/prompt-templates/{template_id}/bind",
            json={"version": 2, "variables": {"role": "诗人"}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["config"]["prompt_version"] == 2
        response = await client.post(
            f"/api/agents/{agent_id}/prompt-templates/{template_id}/compare",
            json={
                "version_a": 1,
                "version_b": 2,
                "cases": [{"input": "hello", "expected": "Mock response (no upstream call)."}],
                "scorer_name": "answer",
            },
        )
        assert response.status_code == 200, response.text
        comparison = response.json()
        assert comparison["version_a"]["average_score"] == 1.0
        assert comparison["version_b"]["average_score"] == 1.0
        assert comparison["delta"]["average_score"] == 0.0
        assert comparison["version_a"]["eval_run_id"] != comparison["version_b"]["eval_run_id"]

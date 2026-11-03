"""Direction 3: local providers, secret encryption, redaction, and offline helpers."""
from __future__ import annotations

import json
import logging
import uuid

import pytest
import httpx
from httpx import ASGITransport, AsyncClient

from app.agent.loop import AgentLoop
from app.database import async_session_factory
from app.llm.client import LLMClient
from app.main import create_app
from app.models import Agent
from app.observability import JsonFormatter
from app.secrets import protect_config, reveal_config


@pytest.mark.asyncio
async def test_sensitive_agent_config_is_encrypted_and_redacted() -> None:
    secret = "test-local-private-value-123456"
    password = "database-password-123"
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.post("/api/agents", json={
            "name": f"secret-{uuid.uuid4()}", "provider": "mock", "model": "mock",
            "config": {"api_key": secret, "nested": {"password": password}, "safe": "visible"},
        })
        assert response.status_code == 201, response.text
        agent_id = response.json()["id"]
        assert response.json()["config"]["api_key"] == "********"
        assert response.json()["config"]["nested"]["password"] == "********"
        assert secret not in response.text and password not in response.text
        async with async_session_factory() as session:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            assert secret not in agent.config and password not in agent.config
            assert "enc:v1:" in agent.config
            revealed = reveal_config(json.loads(agent.config))
            assert revealed["api_key"] == secret
            assert revealed["nested"]["password"] == password
            previous = agent.config
        updated = await client.patch(f"/api/agents/{agent_id}", json={
            "config": {"api_key": "********", "nested": {"password": "********"}, "safe": "changed"},
        })
        assert updated.status_code == 200, updated.text
        async with async_session_factory() as session:
            agent = await session.get(Agent, agent_id)
            assert json.loads(agent.config)["api_key"] == json.loads(previous)["api_key"]
            # Non-secret edits still create a version without exposing old keys.
            revealed = reveal_config(json.loads(agent.config))
            assert revealed["safe"] == "changed"  # config replacement preserves only redacted secrets


def test_json_log_redacts_credentials_and_chinese_identifiers() -> None:
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1,
        "Bearer abc.def.ghi api_key=supersecretvalue phone=13812345678 id=11010519491231002X", (), None,
    )
    output = JsonFormatter().format(record)
    for value in ("abc.def.ghi", "supersecretvalue", "13812345678", "11010519491231002X"):
        assert value not in output
    assert "redacted" in output


@pytest.mark.asyncio
async def test_ollama_uses_openai_compatible_endpoint_without_mock(monkeypatch, db_session) -> None:
    monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
    secret = "local-private-key-123"
    agent = Agent(
        id=str(uuid.uuid4()), name="local", provider="ollama", model="qwen2.5:3b",
        temperature=0, max_steps=2, tools="[]",
        config=json.dumps(protect_config({"base_url": "http://ollama.test/v1", "api_key": secret})),
    )
    loop = AgentLoop(agent, db_session)
    assert isinstance(loop.llm, LLMClient)
    assert loop.llm.base_url == "http://ollama.test/v1"
    assert loop.llm.api_key == secret
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "id": "chat-local", "object": "chat.completion", "created": 1, "model": "qwen2.5:3b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "LOCAL_OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        })

    await loop.llm._client.aclose()
    loop.llm._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
    )
    result = await loop.llm.chat([{"role": "user", "content": "ping"}])
    assert result.content == "LOCAL_OK"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "http://ollama.test/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {secret}"
    assert json.loads(request.content)["model"] == "qwen2.5:3b"
    await loop.llm.close()


@pytest.mark.asyncio
async def test_vllm_and_missing_local_endpoint_are_explicit(monkeypatch, db_session) -> None:
    monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
    agent = Agent(name="vllm", provider="vllm", model="local-qwen", temperature=0,
                  max_steps=2, tools="[]", config=json.dumps({"base_url": "http://vllm.test/v1"}))
    loop = AgentLoop(agent, db_session)
    assert loop.llm.base_url == "http://vllm.test/v1"
    assert loop.llm.api_key == "local-not-required"
    await loop.llm.close()
    agent.provider = "local"
    agent.config = "{}"
    with pytest.raises(ValueError, match="config.base_url"):
        AgentLoop(agent, db_session)

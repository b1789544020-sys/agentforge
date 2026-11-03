"""Regression tests for the production-readiness stage 0 work items."""
from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from app import config as config_module
from app.agent.loop import AgentLoop, LoopConfig
from app.agent.tools import ToolExecutor, ToolRegistry, ToolSpec
from app.database import async_session_factory
from app.llm.client import ChatResult, ToolCall
from app.main import create_app
from app.models import Agent, Run


ROOT = Path(__file__).resolve().parents[2]


class _AlwaysToolLLM:
    async def chat(self, *_args: Any, **_kwargs: Any) -> ChatResult:
        return ChatResult(
            content="",
            tool_calls=[ToolCall(id="again", name="echo", arguments={"text": "again"})],
            prompt_tokens=1,
            completion_tokens=1,
        )


@pytest.mark.asyncio
async def test_production_loop_max_steps_is_terminal(db_session) -> None:
    agent = Agent(name="limit", provider="mock", model="mock", max_steps=2, tools='["echo"]')
    db_session.add(agent)
    await db_session.commit()

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="echo",
            description="echo",
            handler=lambda text: {"text": text},
            schema={"type": "object", "properties": {"text": {"type": "string"}}},
        )
    )
    loop = AgentLoop(
        agent,
        db_session,
        llm=_AlwaysToolLLM(),
        tools=registry,
        tool_executor=ToolExecutor(registry),
        config=LoopConfig(max_steps=2),
    )
    result = await loop.run("never finish")

    assert result.status == "terminated"
    assert result.error == "max_steps reached"
    run = await db_session.get(Run, result.run_id)
    assert run is not None
    assert run.status == "terminated"
    assert run.error == "max_steps reached"


@pytest.mark.asyncio
async def test_run_overrides_are_snapshotted_without_mutating_agent() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/agents",
            json={
                "name": "immutable",
                "provider": "mock",
                "model": "base-model",
                "temperature": 0.2,
                "max_steps": 7,
                "tools": [],
            },
        )
        assert response.status_code == 201, response.text
        agent_id = response.json()["id"]
        response = await client.post(
            f"/api/agents/{agent_id}/runs",
            json={"input": "hello", "model": "run-model", "temperature": 0.9, "max_steps": 3},
        )
        assert response.status_code == 200, response.text
        run_id = response.json()["id"]

    async with async_session_factory() as session:
        agent = await session.get(Agent, agent_id)
        run = await session.get(Run, run_id)
        assert agent is not None and run is not None
        assert (agent.model, agent.temperature, agent.max_steps) == ("base-model", 0.2, 7)
        assert json.loads(run.config) == {
            "model": "run-model",
            "temperature": 0.9,
            "max_steps": 3,
        }


def test_only_one_agent_loop_v2_definition_remains() -> None:
    source = (ROOT / "backend" / "app" / "agent" / "loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentLoopV2"]
    assert len(definitions) == 1


def test_compose_defaults_are_accepted_by_the_application() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["agentforge-api"]["environment"]

    def default_value(value: str) -> str:
        return value.split(":-", 1)[1][:-1] if value.startswith("${") else value

    assert default_value(environment["AGENTFORGE_DATABASE_URL"]).startswith("postgresql+asyncpg://")
    assert default_value(environment["AGENTFORGE_VECTOR_BACKEND"]) == "inprocess"
    assert "postgres" in compose["services"]


@pytest.mark.asyncio
async def test_dangerous_tool_is_denied_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_ENABLE_DANGEROUS_TOOLS", "false")
    config_module.get_settings.cache_clear()
    try:
        executor = ToolExecutor(ToolRegistry)
        with pytest.raises(PermissionError, match="dangerous"):
            await executor.execute("shell_exec", {"command": "definitely-not-a-real-command"})
    finally:
        config_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_static_token_protects_api_but_not_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_API_TOKEN", "stage0-secret")
    config_module.get_settings.cache_clear()
    try:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/health")).status_code == 200
            assert (await client.get("/api/agents")).status_code == 401
            assert (await client.get("/api/agents", headers={"X-API-Key": "wrong"})).status_code == 401
            assert (
                await client.get("/api/agents", headers={"Authorization": "Bearer stage0-secret"})
            ).status_code == 200
            response = await client.post(
                "/api/agents",
                headers={"X-API-Key": "stage0-secret"},
                json={"name": "authenticated", "provider": "mock", "tools": []},
            )
            assert response.status_code == 201, response.text
    finally:
        config_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_file_tools_enforce_directory_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("AGENTFORGE_ENABLE_DANGEROUS_TOOLS", "true")
    monkeypatch.setenv("AGENTFORGE_FILE_ALLOWED_DIRS", json.dumps([str(allowed)]))
    config_module.get_settings.cache_clear()
    try:
        executor = ToolExecutor(ToolRegistry)
        result = await executor.execute(
            "file_write", {"path": str(allowed / "note.txt"), "content": "safe"}
        )
        assert result["bytes_written"] == 4
        assert (await executor.execute("file_read", {"path": str(allowed / "note.txt")}))["content"] == "safe"
        with pytest.raises(Exception, match="outside the allowlist"):
            await executor.execute(
                "file_write", {"path": str(outside / "denied.txt"), "content": "no"}
            )
        assert not (outside / "denied.txt").exists()
    finally:
        config_module.get_settings.cache_clear()


def test_http_host_allowlist_is_checked_before_io(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.tools import _validate_http_url

    monkeypatch.setenv("AGENTFORGE_HTTP_ALLOWED_HOSTS", json.dumps(["example.com", "*.example.org"]))
    config_module.get_settings.cache_clear()
    try:
        _validate_http_url("https://example.com/path")
        _validate_http_url("https://api.example.org/path")
        with pytest.raises(Exception, match="not in the allowlist"):
            _validate_http_url("http://127.0.0.1/admin")
        with pytest.raises(Exception, match="not in the allowlist"):
            _validate_http_url("https://example.org/path")
    finally:
        config_module.get_settings.cache_clear()


def test_websocket_uses_the_same_static_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_API_TOKEN", "ws-secret")
    config_module.get_settings.cache_clear()
    encoded = base64.urlsafe_b64encode(b"ws-secret").decode().rstrip("=")
    try:
        with TestClient(create_app()) as client:
            with pytest.raises(WebSocketDisconnect) as denied:
                with client.websocket_connect("/api/agents/missing/runs/missing/ws"):
                    pass
            assert denied.value.code == 4401
            with pytest.raises(WebSocketDisconnect) as missing:
                with client.websocket_connect(
                    "/api/agents/missing/runs/missing/ws",
                    subprotocols=["agentforge", f"bearer.{encoded}"],
                ):
                    pass
            assert missing.value.code == 4404
    finally:
        config_module.get_settings.cache_clear()

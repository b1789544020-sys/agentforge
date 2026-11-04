"""Stage 4 multi-user authorization and sandbox boundary tests."""
from __future__ import annotations

from typing import Any
import base64
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from httpx import ASGITransport, AsyncClient

from app import config as config_module
from app.agent.sandbox import DockerSandbox, SandboxError, get_sandbox, reset_sandbox
from app.main import create_app
from app.database import Base, async_engine


@pytest.mark.asyncio
async def test_jwt_roles_and_agent_data_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    # This test specifically exercises first-user bootstrap. Earlier modules
    # share the in-memory engine, so arrange an empty database explicitly.
    async with async_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    monkeypatch.setenv("AGENTFORGE_AUTH_ENABLED", "true")
    monkeypatch.setenv("AGENTFORGE_API_TOKEN", "")
    monkeypatch.setenv("AGENTFORGE_SECRET_KEY", "stage4-test-secret")
    config_module.get_settings.cache_clear()
    app = create_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            admin_response = await client.post(
                "/api/auth/register", json={"email": "admin@example.com", "password": "admin-password"}
            )
            assert admin_response.status_code == 201, admin_response.text
            admin_token = admin_response.json()["access_token"]
            assert admin_response.json()["user"]["role"] == "admin"
            user_response = await client.post(
                "/api/auth/register", json={"email": "user@example.com", "password": "user-password"}
            )
            assert user_response.status_code == 201, user_response.text
            user_token = user_response.json()["access_token"]
            assert user_response.json()["user"]["role"] == "user"
            admin_headers = {"Authorization": f"Bearer {admin_token}"}
            user_headers = {"Authorization": f"Bearer {user_token}"}

            created = await client.post(
                "/api/agents", headers=user_headers,
                json={"name": "private-agent", "provider": "mock", "model": "mock", "tools": []},
            )
            assert created.status_code == 201, created.text
            agent_id = created.json()["id"]
            foreign = await client.post(
                "/api/agents", headers=admin_headers,
                json={"name": "admin-agent", "provider": "mock", "model": "mock", "tools": []},
            )
            foreign_id = foreign.json()["id"]
            assert (await client.get(f"/api/agents/{foreign_id}", headers=user_headers)).status_code == 404
            assert (await client.post(
                f"/api/agents/{foreign_id}/runs", headers=user_headers, json={"input": "no"}
            )).status_code == 404
            assert (await client.get(f"/api/agents/{foreign_id}/memory", headers=user_headers)).status_code == 404
            visible = (await client.get("/api/agents", headers=user_headers)).json()
            assert {row["id"] for row in visible} == {agent_id}
            admin_ids = {row["id"] for row in (await client.get("/api/agents", headers=admin_headers)).json()}
            assert {agent_id, foreign_id}.issubset(admin_ids)
            assert (await client.get("/api/admin/users", headers=admin_headers)).status_code == 200
            assert (await client.get("/api/admin/users", headers=user_headers)).status_code == 403
            assert (await client.put(
                "/api/tools/echo/metadata", headers=user_headers, json={"description": "no"}
            )).status_code == 403

            login = await client.post(
                "/api/auth/login", json={"email": "user@example.com", "password": "user-password"}
            )
            assert login.status_code == 200, login.text
            me = await client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
            )
            assert me.status_code == 200 and me.json()["email"] == "user@example.com"
    finally:
        config_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_sandbox_required_never_falls_back_to_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_SANDBOX_REQUIRED", "true")
    monkeypatch.setenv("AGENTFORGE_SANDBOX_ENABLED", "false")
    config_module.get_settings.cache_clear()
    monkeypatch.setattr(
        "app.agent.sandbox.DockerSandbox",
        lambda: (_ for _ in ()).throw(SandboxError("daemon unavailable")),
    )
    await reset_sandbox()
    try:
        with pytest.raises(SandboxError, match="daemon unavailable"):
            get_sandbox()
    finally:
        await reset_sandbox()
        config_module.get_settings.cache_clear()


class _FakeContainer:
    def wait(self, timeout: float) -> dict[str, int]:
        assert timeout > 0
        return {"StatusCode": 0}

    def logs(self, stdout: bool, stderr: bool) -> bytes:
        return b"ok" if stdout else b""

    def remove(self, force: bool) -> None:
        assert force


class _FakeContainers:
    def __init__(self) -> None:
        self.options: dict[str, Any] = {}

    def run(self, **options: Any) -> _FakeContainer:
        self.options = options
        return _FakeContainer()


class _FakeDockerClient:
    def __init__(self) -> None:
        self.containers = _FakeContainers()


@pytest.mark.asyncio
async def test_docker_sandbox_enforces_container_security_options() -> None:
    client = _FakeDockerClient()
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.image = "sandbox-image"
    sandbox.memory = "256m"
    sandbox.cpu_quota = 1.0
    sandbox.network_disabled = True
    sandbox.timeout = 5
    sandbox.max_output_bytes = 1024
    sandbox._client = client
    result = await sandbox.run_python("print('ok')")
    assert result.exit_code == 0 and result.stdout == "ok"
    options = client.containers.options
    assert options["read_only"] is True
    assert options["network_disabled"] is True
    assert options["cap_drop"] == ["ALL"]
    assert options["pids_limit"] == 128
    assert options["user"] == "10001:10001"
    assert "no-new-privileges:true" in options["security_opt"]
    assert "volumes" not in options
    assert options["entrypoint"] == ["python", "-I"]


def test_websocket_enforces_agent_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_AUTH_ENABLED", "true")
    monkeypatch.setenv("AGENTFORGE_API_TOKEN", "")
    monkeypatch.setenv("AGENTFORGE_SECRET_KEY", "stage4-ws-secret")
    config_module.get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            admin = client.post(
                "/api/auth/register", json={"email": "ws-admin@example.com", "password": "admin-password"}
            ).json()
            user = client.post(
                "/api/auth/register", json={"email": "ws-user@example.com", "password": "user-password"}
            ).json()
            headers = {"Authorization": f"Bearer {admin['access_token']}"}
            agent = client.post(
                "/api/agents", headers=headers,
                json={"name": "ws-private", "provider": "mock", "model": "mock", "tools": []},
            ).json()
            run = client.post(
                f"/api/agents/{agent['id']}/runs", headers=headers, json={"input": "hello"}
            ).json()
            encoded = base64.urlsafe_b64encode(user["access_token"].encode()).decode().rstrip("=")
            with pytest.raises(WebSocketDisconnect) as denied:
                with client.websocket_connect(
                    f"/api/agents/{agent['id']}/runs/{run['id']}/ws",
                    subprotocols=["agentforge", f"bearer.{encoded}"],
                ):
                    pass
            assert denied.value.code == 4404
    finally:
        config_module.get_settings.cache_clear()


def test_compose_enables_auth_and_requires_real_sandbox() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["agentforge-api"]["environment"]
    assert environment["AGENTFORGE_AUTH_ENABLED"].endswith("true}")
    assert environment["AGENTFORGE_SANDBOX_REQUIRED"].endswith("true}")
    assert ":?" in environment["AGENTFORGE_SECRET_KEY"]

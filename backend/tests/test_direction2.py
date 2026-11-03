"""Direction 2: team RBAC, hard tenant isolation, audit, and approvals."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import config as config_module
from app.database import async_session_factory
from app.main import create_app
from app.models import AuditLog, EvalCase, ToolMetadata, User
from app.rbac import ROLE_PERMISSIONS
from app.security import create_access_token


async def _users() -> dict[str, tuple[str, str, str]]:
    result: dict[str, tuple[str, str, str]] = {}
    async with async_session_factory() as session:
        for name, role in (("root", "admin"), ("admin1", "user"), ("dev1", "user"),
                           ("viewer1", "user"), ("dev2", "user")):
            user = User(id=str(uuid.uuid4()), email=f"{name}-{uuid.uuid4()}@test.local",
                        password_hash="unused", role=role)
            session.add(user)
            result[name] = (user.id, user.email, "")
        await session.commit()
    for name, (user_id, email, _empty) in list(result.items()):
        result[name] = (user_id, email, create_access_token(user_id, "admin" if name == "root" else "user"))
    return result


@pytest.fixture
async def team_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTFORGE_AUTH_ENABLED", "true")
    monkeypatch.setenv("AGENTFORGE_API_TOKEN", "")
    monkeypatch.setenv("AGENTFORGE_SECRET_KEY", "direction2-test-secret")
    config_module.get_settings.cache_clear()
    users = await _users()
    client = AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")
    try:
        yield client, users
    finally:
        await client.aclose()
        config_module.get_settings.cache_clear()



def _headers(users, name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {users[name][2]}"}




@pytest.mark.asyncio
async def test_team_roles_and_cross_team_resource_isolation(team_env) -> None:
    client, users = team_env
    root = _headers(users, "root")
    org = await client.post("/api/orgs", headers=root, json={"name": f"org-{uuid.uuid4()}"})
    assert org.status_code == 201, org.text
    team_ids: list[str] = []
    for name in ("customer", "legal"):
        response = await client.post("/api/teams", headers=root, json={"org_id": org.json()["id"], "name": name})
        assert response.status_code == 201, response.text
        team_ids.append(response.json()["id"])
    team1, team2 = team_ids
    for team_id, names in ((team1, ("admin1", "dev1", "viewer1")), (team2, ("dev2",))):
        for name in names:
            role = "team-admin" if name == "admin1" else "viewer" if name == "viewer1" else "developer"
            response = await client.post(f"/api/teams/{team_id}/members", headers=root,
                                         json={"email": users[name][1], "role": role})
            assert response.status_code == 201, response.text

    dev1, dev2, viewer = _headers(users, "dev1"), _headers(users, "dev2"), _headers(users, "viewer1")
    denied = await client.post("/api/agents", headers=viewer, json={"name": "viewer-agent", "team_id": team1})
    assert denied.status_code == 403
    agent_ids: dict[str, str] = {}
    for name, headers, team_id in (("a1", dev1, team1), ("a2", dev2, team2)):
        response = await client.post("/api/agents", headers=headers, json={
            "name": name, "team_id": team_id, "provider": "mock", "model": "mock", "tools": [],
        })
        assert response.status_code == 201, response.text
        assert response.json()["team_id"] == team_id
        agent_ids[name] = response.json()["id"]
    a1, a2 = agent_ids["a1"], agent_ids["a2"]

    run = await client.post(f"/api/agents/{a1}/runs", headers=dev1, json={"input": "private"})
    assert run.status_code == 200, run.text
    memory = await client.post(f"/api/agents/{a1}/memory", headers=dev1, json={"content": "team1 secret"})
    assert memory.status_code == 201, memory.text
    evaluation = await client.post(f"/api/agents/{a1}/evals", headers=dev1, json={
        "scorer_name": "status", "cases": [{"input": "private", "expected": "anything"}],
    })
    assert evaluation.status_code == 201, evaluation.text

    for path in (f"/api/agents/{a1}", f"/api/agents/{a1}/runs/{run.json()['id']}",
                 f"/api/agents/{a1}/evals/{evaluation.json()['id']}", f"/api/agents/{a1}/memory"):
        response = await client.get(path, headers=dev2)
        assert response.status_code in {403, 404}, (path, response.status_code, response.text)
    assert (await client.post(f"/api/agents/{a1}/runs", headers=dev2, json={"input": "steal"})).status_code == 404
    visible = (await client.get("/api/agents", headers=dev2)).json()
    assert {row["id"] for row in visible} == {a2}
    assert a2 not in {row["id"] for row in (await client.get("/api/agents", headers=dev1)).json()}
    assert {row["id"] for row in (await client.get("/api/teams", headers=dev1)).json()} == {team1}
    assert {row["id"] for row in (await client.get("/api/teams", headers=dev2)).json()} == {team2}
    assert (await client.get(f"/api/agents/{a1}", headers=_headers(users, "admin1"))).status_code == 200
    assert (await client.post(f"/api/teams/{team1}/members", headers=viewer,
                              json={"email": users["dev2"][1], "role": "viewer"})).status_code == 403


@pytest.mark.asyncio
async def test_audit_immutability_and_dangerous_tool_approval(team_env) -> None:
    client, users = team_env
    root, dev1, admin1 = _headers(users, "root"), _headers(users, "dev1"), _headers(users, "admin1")
    org = (await client.post("/api/orgs", headers=root, json={"name": f"audit-{uuid.uuid4()}"})).json()
    team = (await client.post("/api/teams", headers=root, json={"org_id": org["id"], "name": "audit-team"})).json()
    for name, role in (("admin1", "team-admin"), ("dev1", "developer")):
        await client.post(f"/api/teams/{team['id']}/members", headers=root,
                          json={"email": users[name][1], "role": role})
    created = await client.post("/api/agents", headers=dev1, json={
        "name": "delete-me", "team_id": team["id"], "provider": "mock", "model": "mock",
    })
    agent_id = created.json()["id"]
    deleted = await client.delete(f"/api/agents/{agent_id}", headers=admin1)
    assert deleted.status_code == 204, deleted.text
    audits = await client.get("/api/audit", headers=admin1, params={"team_id": team["id"], "action": "agent.delete"})
    assert audits.status_code == 200, audits.text
    assert any(row["target_id"] == agent_id and row["actor_id"] == users["admin1"][0] for row in audits.json())
    assert (await client.get("/api/audit", headers=dev1, params={"team_id": team["id"]})).status_code == 403

    async with async_session_factory() as session:
        session.add(ToolMetadata(name="shell_exec", team_id=team["id"], enabled=False))
        await session.commit()
    pending = await client.post("/api/tools/shell_exec/enable-request", headers=dev1,
                                json={"team_id": team["id"], "reason": "Required for controlled release automation"})
    assert pending.status_code == 202, pending.text
    approval_id = pending.json()["approval_id"]
    async with async_session_factory() as session:
        assert (await session.get(ToolMetadata, "shell_exec")).enabled is False
    approved = await client.post(f"/api/approvals/{approval_id}/approve", headers=admin1,
                                 json={"reason": "Approved after sandbox policy review"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    async with async_session_factory() as session:
        assert (await session.get(ToolMetadata, "shell_exec")).enabled is True
        row = (await session.execute(select(AuditLog).where(AuditLog.action == "tool.enable"))).scalars().first()
        assert row is not None
        row.action = "tampered"
        with pytest.raises(ValueError, match="append-only"):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_suite_scope_and_developer_delete_boundaries(team_env) -> None:
    client, users = team_env
    root, dev1, dev2, admin1 = (_headers(users, name) for name in ("root", "dev1", "dev2", "admin1"))
    org = (await client.post("/api/orgs", headers=root, json={"name": f"scope-{uuid.uuid4()}"})).json()
    teams = []
    for name in ("scope-one", "scope-two"):
        teams.append((await client.post("/api/teams", headers=root,
                                       json={"org_id": org["id"], "name": name})).json())
    for team, name, role in ((teams[0], "admin1", "team-admin"), (teams[0], "dev1", "developer"),
                             (teams[1], "dev2", "developer")):
        await client.post(f"/api/teams/{team['id']}/members", headers=root,
                          json={"email": users[name][1], "role": role})
    case_ids = []
    async with async_session_factory() as session:
        for team in teams:
            case_id = str(uuid.uuid4())
            case_ids.append(case_id)
            session.add(EvalCase(id=case_id, team_id=team["id"], dataset="scope", name=team["name"],
                                 input="question", expected="answer", meta="{}", tool_calls="[]", score_details="{}"))
        await session.commit()
    agent = await client.post("/api/agents", headers=dev1, json={
        "name": "suite-owner", "team_id": teams[0]["id"], "provider": "mock", "model": "mock",
    })
    agent_id = agent.json()["id"]
    rejected = await client.post("/api/suites", headers=dev1, json={
        "name": "foreign-case", "agent_id": agent_id, "case_ids": [case_ids[1]],
    })
    assert rejected.status_code == 404
    suite = await client.post("/api/suites", headers=dev1, json={
        "name": "own-case", "agent_id": agent_id, "case_ids": [case_ids[0]], "auto_on_save": False,
    })
    assert suite.status_code == 201, suite.text
    assert (await client.get(f"/api/suites/{suite.json()['id']}", headers=dev2)).status_code == 404
    visible = (await client.get("/api/suites", headers=dev2)).json()
    assert suite.json()["id"] not in {row["id"] for row in visible}

    foreign = await client.post("/api/agents", headers=admin1, json={
        "name": "admin-owned", "team_id": teams[0]["id"], "provider": "mock", "model": "mock",
    })
    assert (await client.delete(f"/api/agents/{foreign.json()['id']}", headers=dev1)).status_code == 404
    own = await client.post("/api/agents", headers=dev1, json={
        "name": "developer-owned", "team_id": teams[0]["id"], "provider": "mock", "model": "mock",
    })
    assert (await client.delete(f"/api/agents/{own.json()['id']}", headers=dev1)).status_code == 204


def test_permission_matrix_is_central_and_complete() -> None:
    assert "agent:create" not in ROLE_PERMISSIONS["viewer"]
    assert "agent:create" in ROLE_PERMISSIONS["developer"]
    assert "member:manage" in ROLE_PERMISSIONS["team-admin"]
    assert "gate:override" not in ROLE_PERMISSIONS["developer"]
    assert "*" in ROLE_PERMISSIONS["super-admin"]

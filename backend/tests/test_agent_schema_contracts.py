"""Agent Schema v1 backend prerequisites (roadmap 16.8)."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from app.agent.schema import assert_public_safe, public_definition, public_legacy_dto
from app.agent.versions import agent_from_version
from app.database import async_session_factory
from app.main import create_app
from app.models import Agent, AgentVersion, KnowledgeBase
from app.prompting import resolve_system_prompt
from app.secrets import SecretPathConflictError, protect_config, reveal_config
from httpx import ASGITransport, AsyncClient

FIXTURES = Path(__file__).parent / "fixtures" / "agent_schema"
MASK = "********"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def _create_agent(client: AsyncClient, name: str = "schema-agent", **overrides) -> dict:
    payload = {
        "name": name,
        "provider": "mock",
        "model": "mock-model",
        "temperature": 0,
        "max_steps": 2,
        "tools": [],
        "config": {"mock_replies": {"question": f"answer-{name}"}},
        **overrides,
    }
    response = await client.post("/api/agents", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _versions(client: AsyncClient, agent_id: str) -> list[dict]:
    response = await client.get(f"/api/agents/{agent_id}/versions")
    assert response.status_code == 200, response.text
    return response.json()


def test_legacy_snapshot_remains_runtime_compatible() -> None:
    snapshot = _fixture("legacy_snapshot.json")
    version = AgentVersion(
        id="legacy-version", agent_id="legacy-agent-id", team_id=None,
        snapshot=json.dumps(snapshot),
    )
    agent = agent_from_version(version)
    assert agent.temperature == 0
    assert json.loads(agent.config) == snapshot["config"]
    assert agent._agent_version_id == "legacy-version"
    assert not hasattr(agent, "_runtime_snapshot")


@pytest.mark.parametrize("filename", ["legacy_snapshot.json", "security_snapshot.json"])
def test_public_legacy_projection_is_recursive_and_non_mutating(filename: str) -> None:
    snapshot = _fixture(filename)
    original = copy.deepcopy(snapshot)
    projected = public_legacy_dto(snapshot)
    assert snapshot == original
    serialized = json.dumps(projected, ensure_ascii=False)
    assert "ciphertext" not in serialized
    assert "plain-fixture-secret" not in serialized
    assert projected["config"] != snapshot["config"]
    assert_public_safe(projected, secret_values=["plain-fixture-secret"])


def test_public_definition_strips_values_and_escapes_json_pointers() -> None:
    snapshot = _fixture("security_snapshot.json")
    snapshot["config"]["a/b~c"] = {"api_key": "pointer-fixture-secret", "keep": 1}
    original = copy.deepcopy(snapshot)
    definition = public_definition(snapshot)
    assert snapshot == original
    assert definition["schema_version"] == 1
    assert definition["config"]["a/b~c"] == {"keep": 1}
    pointer_ref = next(row for row in definition["secret_refs"] if row["ref"] == "a/b~c.api_key")
    assert pointer_ref == {
        "path": "/config/a~1b~0c/api_key", "ref": "a/b~c.api_key", "required": False,
    }
    assert "content" not in definition["resources"]
    assert_public_safe(definition, secret_values=["pointer-fixture-secret"])


def test_public_safety_assertion_fails_closed() -> None:
    for unsafe in (
        {"nested": [{"api_key": "plaintext"}]},
        {"nested": [{"value": "enc:v99:unknown"}]},
    ):
        with pytest.raises(ValueError, match="unsafe public"):
            assert_public_safe(unsafe)


def test_masked_secret_merge_preserves_clears_and_rejects_ambiguity() -> None:
    previous = protect_config({"connections": [
        {"id": "a", "api_key": "connection-a-secret"},
        {"id": "b", "api_key": "connection-b-secret"},
    ]})
    preserved = protect_config({"connections": [
        {"id": "a", "api_key": MASK},
        {"id": "b", "api_key": MASK},
    ]}, previous)
    assert reveal_config(preserved)["connections"][0]["api_key"] == "connection-a-secret"
    assert "api_key" not in protect_config({"api_key": MASK})
    assert protect_config({"api_key": None}, previous)["api_key"] is None
    assert protect_config({"api_key": ""}, previous)["api_key"] == ""

    for replacement in (
        {"connections": [{"id": "a", "api_key": MASK}]},
        {"connections": [
            {"id": "b", "api_key": MASK}, {"id": "a", "api_key": MASK},
        ]},
    ):
        with pytest.raises(SecretPathConflictError) as caught:
            protect_config(replacement, previous)
        assert "secret" not in str(caught.value).lower().replace("masked secret", "")



@pytest.mark.asyncio
async def test_http_secret_conflict_is_422_without_version_or_secret_leak() -> None:
    async with _client() as client:
        agent = await _create_agent(client, config={"connections": [
            {"id": "a", "api_key": "connection-a-secret"},
            {"id": "b", "api_key": "connection-b-secret"},
        ]})
        agent_id = agent["id"]
        response = await client.patch(f"/api/agents/{agent_id}", json={"config": {
            "connections": [{"id": "a", "api_key": MASK}],
        }})
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["error"] == "secret_path_conflict"
        assert response.json()["detail"]["path"] == "/connections"
        assert "connection-a-secret" not in response.text
        assert "connection-b-secret" not in response.text
        assert len(await _versions(client, agent_id)) == 1

        response = await client.patch(f"/api/agents/{agent_id}", json={"config": {
            "connections": [
                {"id": "b", "api_key": MASK}, {"id": "a", "api_key": MASK},
            ],
        }})
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["path"] == "/connections/0"
        assert len(await _versions(client, agent_id)) == 1


@pytest.mark.asyncio
async def test_run_executes_selected_version_and_rejects_cross_agent_version() -> None:
    async with _client() as client:
        agent = await _create_agent(client, "versioned")
        agent_id = agent["id"]
        version1 = (await _versions(client, agent_id))[0]
        updated = await client.patch(f"/api/agents/{agent_id}", json={
            "config": {"mock_replies": {"question": "answer-v2"}},
        })
        assert updated.status_code == 200, updated.text
        version2 = (await _versions(client, agent_id))[0]
        assert version2["id"] != version1["id"]

        old_run = await client.post(f"/api/agents/{agent_id}/runs", json={
            "input": "question", "agent_version_id": version1["id"],
        })
        assert old_run.status_code == 200, old_run.text
        assert old_run.json()["output"] == "answer-versioned"
        assert old_run.json()["agent_version_id"] == version1["id"]

        latest_run = await client.post(f"/api/agents/{agent_id}/runs", json={"input": "question"})
        assert latest_run.status_code == 200, latest_run.text
        assert latest_run.json()["output"] == "answer-v2"
        assert latest_run.json()["agent_version_id"] == version2["id"]

        other = await _create_agent(client, "other")
        other_version = (await _versions(client, other["id"]))[0]
        rejected = await client.post(f"/api/agents/{agent_id}/runs", json={
            "input": "question", "agent_version_id": other_version["id"],
        })
        assert rejected.status_code == 404


@pytest.mark.asyncio
async def test_prompt_content_and_hash_are_pinned_in_version() -> None:
    async with _client() as client:
        agent = await _create_agent(client, "prompt-pin")
        agent_id = agent["id"]
        created = await client.post(f"/api/agents/{agent_id}/prompt-templates", json={
            "name": "research", "template": "v1 {{ audience }} / {{ input }}",
        })
        assert created.status_code == 201, created.text
        template_id = created.json()["id"]
        bound = await client.post(
            f"/api/agents/{agent_id}/prompt-templates/{template_id}/bind",
            json={"version": 1, "variables": {"audience": "开发者"}},
        )
        assert bound.status_code == 200, bound.text
        version_id = bound.json()["agent_version_id"]
        new_prompt = await client.post(
            f"/api/agents/{agent_id}/prompt-templates/{template_id}/versions",
            json={"template": "v2 {{ audience }} / {{ input }}"},
        )
        assert new_prompt.status_code == 201, new_prompt.text

        async with async_session_factory() as session:
            version = await session.get(AgentVersion, version_id)
            snapshot = json.loads(version.snapshot)
            pinned = snapshot["resources"]["prompt"]
            assert pinned["version"] == 1
            assert pinned["content"] == "v1 {{ audience }} / {{ input }}"
            assert pinned["content_sha256"] == hashlib.sha256(
                pinned["content"].encode("utf-8")
            ).hexdigest()
            runtime_agent = agent_from_version(version)
            rendered = await resolve_system_prompt(session, runtime_agent, "问题")
            assert rendered == "v1 开发者 / 问题"

            snapshot["resources"]["prompt"]["content"] = "tampered"
            tampered = AgentVersion(
                id=version.id, agent_id=version.agent_id, team_id=version.team_id,
                snapshot=json.dumps(snapshot),
            )
            with pytest.raises(ValueError, match="hash mismatch"):
                await resolve_system_prompt(session, agent_from_version(tampered), "问题")


@pytest.mark.asyncio
async def test_knowledge_binding_snapshot_survives_unbind_and_missing_current_resource() -> None:
    from app.agent.loop import AgentLoop
    from app.api.deployments import _spec

    async with _client() as client:
        agent = await _create_agent(client, "knowledge-pin")
        agent_id = agent["id"]
        kb_ids = []
        for name in ("kb-a", "kb-b"):
            created = await client.post("/api/knowledge-bases", json={"name": name})
            assert created.status_code == 201, created.text
            kb_ids.append(created.json()["id"])
        # Refs remain stable across sequential saves; snapshots are serialized by ref.
        for kb_id in reversed(kb_ids):
            bound = await client.put(f"/api/agents/{agent_id}/knowledge/{kb_id}")
            assert bound.status_code == 201, bound.text
        pinned_version_id = bound.json()["agent_version_id"]

        async with async_session_factory() as session:
            pinned_version = await session.get(AgentVersion, pinned_version_id)
            pinned_snapshot = json.loads(pinned_version.snapshot)
            pinned_knowledge = pinned_snapshot["resources"]["knowledge_bases"]
            assert {row["kb_id"] for row in pinned_knowledge} == set(kb_ids)
            assert [row["ref"] for row in pinned_knowledge] == ["kb-1", "kb-2"]
            assert pinned_knowledge[0]["kb_id"] == kb_ids[1]
            pinned_config = json.loads(agent_from_version(pinned_version).config)
            assert pinned_config["knowledge_base_ids"] == sorted(kb_ids)
            deployment_spec = await _spec(session, pinned_version)
            assert deployment_spec["knowledge_base_ids"] == sorted(kb_ids)

        unbound = await client.delete(f"/api/agents/{agent_id}/knowledge/{kb_ids[0]}")
        assert unbound.status_code == 204, unbound.text
        latest = (await _versions(client, agent_id))[0]
        assert [row["kb_id"] for row in latest["snapshot"]["resources"]["knowledge_bases"]] == [kb_ids[1]]

        async with async_session_factory() as session:
            pinned_version = await session.get(AgentVersion, pinned_version_id)
            assert json.loads(agent_from_version(pinned_version).config)["knowledge_base_ids"] == sorted(kb_ids)
            missing = await session.get(KnowledgeBase, kb_ids[0])
            await session.delete(missing)
            await session.commit()
            with pytest.raises(ValueError, match="resource_unavailable"):
                await AgentLoop(agent_from_version(pinned_version), session)._build_context("question")


@pytest.mark.asyncio
async def test_public_agent_version_template_and_deployment_surfaces_do_not_leak() -> None:
    secrets = {"public-api-secret", "public-password-secret"}
    async with _client() as client:
        agent = await _create_agent(client, "public-surfaces", config={
            "provider": {"api_key": "public-api-secret"},
            "nested": [{"password": "public-password-secret"}],
            "mock_replies": {"question": "safe"},
        })
        agent_id = agent["id"]
        version = (await _versions(client, agent_id))[0]
        template = await client.post("/api/templates", json={
            "agent_id": agent_id, "name": "sanitized-template",
        })
        assert template.status_code == 201, template.text

        async with async_session_factory() as session:
            row = await session.get(AgentVersion, version["id"])
            row.gate_status = "passed"
            await session.commit()
        published = await client.post("/api/deployments", json={
            "agent_version_id": version["id"], "name": "sanitized-deployment",
        })
        assert published.status_code == 201, published.text
        deployment_id = published.json()["id"]

        responses = [
            await client.get(f"/api/agents/{agent_id}"),
            await client.get(f"/api/agents/{agent_id}/versions/{version['id']}"),
            await client.get(f"/api/agents/{agent_id}/versions/{version['id']}/definition"),
            await client.get("/api/templates"),
            await client.get(f"/api/deployments/{deployment_id}"),
        ]
        for response in responses:
            assert response.status_code == 200, response.text
            assert_public_safe(response.json(), secret_values=secrets)
            for secret in secrets:
                assert secret not in response.text
            assert "enc:v1:" not in response.text

        definition = responses[2].json()
        assert definition["schema_version"] == 1
        assert {row["path"] for row in definition["secret_refs"]} == {
            "/config/provider/api_key", "/config/nested/0/password",
        }


def _legacy_package(name: str, config: dict) -> bytes:
    return json.dumps({
        "version": 1,
        "kind": "agentforge.agent",
        "agents": [{
            "name": name,
            "description": "masked import",
            "system_prompt": "safe",
            "provider": "mock",
            "model": "mock-model",
            "temperature": 0,
            "max_steps": 2,
            "tools": [],
            "config": config,
        }],
    }).encode("utf-8")


@pytest.mark.asyncio
async def test_masked_import_dry_run_reports_missing_without_writing() -> None:
    async with _client() as client:
        files = {"file": (
            "agents.json", _legacy_package("new-masked", {"nested": {"api_key": MASK}}),
            "application/json",
        )}
        preflight = await client.post("/api/agents/import?dry_run=true", files=files)
        assert preflight.status_code == 200, preflight.text
        report = preflight.json()
        assert report["dry_run"] is True
        assert report["imported"] == 1
        assert report["missing_secrets"] == [{
            "agent": "new-masked", "path": "/config/nested/api_key",
            "ref": "nested.api_key", "required": False,
        }]
        assert report["inherited_secrets"] == []
        assert (await client.get("/api/agents")).json() == []

        imported = await client.post("/api/agents/import", files=files)
        assert imported.status_code == 200, imported.text
        assert imported.json()["dry_run"] is False
        assert imported.json()["missing_secrets"] == report["missing_secrets"]
        agent = (await client.get("/api/agents")).json()[0]
        assert agent["config"]["nested"] == {}
        async with async_session_factory() as session:
            stored = await session.get(Agent, agent["id"])
            assert json.loads(stored.config)["nested"] == {}
            assert MASK not in stored.config


@pytest.mark.asyncio
async def test_masked_overwrite_preflight_inherits_existing_secret() -> None:
    async with _client() as client:
        agent = await _create_agent(
            client, "existing-masked", config={"nested": {"api_key": "original-import-secret"}},
        )
        agent_id = agent["id"]
        original_version = (await _versions(client, agent_id))[0]
        files = {"file": (
            "agents.json", _legacy_package("existing-masked", {"nested": {"api_key": MASK}}),
            "application/json",
        )}
        preflight = await client.post(
            "/api/agents/import?dry_run=true", data={"on_conflict": "overwrite"}, files=files,
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["updated"] == 1
        assert preflight.json()["missing_secrets"] == []
        assert preflight.json()["inherited_secrets"] == [{
            "agent": "existing-masked", "path": "/config/nested/api_key",
            "ref": "nested.api_key", "required": False,
        }]
        assert (await _versions(client, agent_id))[0]["id"] == original_version["id"]

        imported = await client.post(
            "/api/agents/import", data={"on_conflict": "overwrite"}, files=files,
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["inherited_secrets"] == preflight.json()["inherited_secrets"]
        assert len(await _versions(client, agent_id)) == 2
        assert "original-import-secret" not in imported.text
        async with async_session_factory() as session:
            stored = await session.get(Agent, agent_id)
            config = reveal_config(json.loads(stored.config))
            assert config["nested"]["api_key"] == "original-import-secret"

"""Canonical Definition, envelope 2, resource mapping, and concurrency contracts."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest
import yaml
from app.database import async_session_factory
from app.main import create_app
from app.models import Agent, AgentVersion, KnowledgeBase, KnowledgeBinding, PromptTemplate, Run
from app.secrets import reveal_config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


def _definition(name: str, **overrides) -> dict:
    row = {
        "schema_version": 1,
        "name": name,
        "description": "canonical test",
        "system_prompt": "answer briefly",
        "provider": "mock",
        "model": "mock-model",
        "temperature": 0,
        "max_steps": 2,
        "tools": [],
        "config": {"mock_replies": {"question": f"answer-{name}"}},
        "resources": {"prompt": None, "knowledge_bases": []},
        "secret_refs": [],
        "extensions": {},
    }
    row.update(overrides)
    return row


def _package(*rows: dict) -> bytes:
    return json.dumps({
        "version": 2, "kind": "agentforge.agent", "agents": list(rows),
    }, ensure_ascii=False).encode("utf-8")


def _files(content: bytes, filename: str = "agents.json") -> dict:
    media_type = "application/x-yaml" if filename.endswith((".yaml", ".yml")) else "application/json"
    return {"file": (filename, content, media_type)}


async def _create_agent(name: str = "current-agent", **overrides) -> dict:
    async with _client() as client:
        response = await client.post("/api/agents", json={
            "name": name, "provider": "mock", "model": "mock-model",
            "temperature": 0, "max_steps": 2, "tools": [],
            "config": {"mock_replies": {"question": f"answer-{name}"}}, **overrides,
        })
        assert response.status_code == 201, response.text
        return response.json()


async def _current(client: AsyncClient, agent_id: str) -> dict:
    response = await client.get(f"/api/v1/agents/{agent_id}/definition")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_default_export_is_envelope2_and_legacy_export_is_explicit() -> None:
    agent = await _create_agent("export-canonical", config={
        "nested": {"api_key": "export-plain-secret"}, "mock_replies": {"question": "answer"},
    })
    async with _client() as client:
        exported = await client.post("/api/agents/export", json={"agent_ids": [agent["id"]]})
        assert exported.status_code == 200, exported.text
        payload = exported.json()
        assert payload["version"] == 2
        row = payload["agents"][0]
        assert row["schema_version"] == 1
        assert row["config"]["nested"] == {}
        assert row["secret_refs"] == [{
            "path": "/config/nested/api_key", "ref": "nested.api_key", "required": False,
        }]
        assert "export-plain-secret" not in exported.text
        assert "enc:v1:" not in exported.text

        legacy = await client.post(
            "/api/agents/export?envelope=1", json={"agent_ids": [agent["id"]]},
        )
        assert legacy.status_code == 200
        assert legacy.json()["version"] == 1
        assert "omits canonical resources" in legacy.headers["x-agentforge-export-warning"]
        assert legacy.json()["agents"][0]["config"]["nested"]["api_key"] == "********"


@pytest.mark.asyncio
async def test_envelope2_unicode_secret_inputs_and_current_definition_round_trip() -> None:
    row = _definition(
        " 研究助手 ",
        config={"a/b~c": {}, "mock_replies": {"question": "答案"}},
        secret_refs=[{"path": "/config/a~1b~0c/api_key", "ref": "provider/主", "required": True}],
        extensions={"future": {"说明": "保留 ✓", "items": [1, {"键": "值"}]}},
    )
    secret = "一次性秘密-不进入响应"
    options = {"secret_inputs": json.dumps({"provider/主": secret}, ensure_ascii=False)}
    async with _client() as client:
        preflight = await client.post(
            "/api/agents/import?dry_run=true", files=_files(_package(row)), data=options,
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["ok"] is True
        assert preflight.json()["missing_secrets"] == []
        assert secret not in preflight.text
        assert (await client.get("/api/agents")).json() == []

        imported = await client.post("/api/agents/import", files=_files(_package(row)), data=options)
        assert imported.status_code == 200, imported.text
        assert secret not in imported.text
        agent = (await client.get("/api/agents")).json()[0]
        assert agent["name"] == "研究助手"
        current = await _current(client, agent["id"])
        assert current["definition"]["config"]["a/b~c"] == {}
        assert current["definition"]["secret_refs"] == row["secret_refs"]
        assert current["definition"]["extensions"] == row["extensions"]
        assert current["missing_secrets"] == []
        assert {item["code"] for item in current["warnings"]} == {"extensions_preserved_not_executed"}
        async with async_session_factory() as session:
            stored = await session.get(Agent, agent["id"])
            config = reveal_config(json.loads(stored.config))
            assert config["a/b~c"]["api_key"] == secret
            version = await session.get(AgentVersion, current["latest_version_id"])
            snapshot = json.loads(version.snapshot)
            assert snapshot["extensions"] == row["extensions"]
            assert snapshot["secret_refs"] == row["secret_refs"]


@pytest.mark.asyncio
async def test_required_secret_is_reported_and_blocks_run_until_supplied() -> None:
    row = _definition(
        "required-secret",
        config={"nested": {}, "mock_replies": {"question": "secret-answer"}},
        secret_refs=[{"path": "/config/nested/api_key", "ref": "required-key", "required": True}],
    )
    async with _client() as client:
        preflight = await client.post("/api/agents/import?dry_run=true", files=_files(_package(row)))
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["ok"] is True
        assert preflight.json()["missing_secrets"] == [{
            "agent": "required-secret", "path": "/config/nested/api_key",
            "ref": "required-key", "required": True,
        }]
        imported = await client.post("/api/agents/import", files=_files(_package(row)))
        assert imported.status_code == 200, imported.text
        agent = (await client.get("/api/agents")).json()[0]
        blocked = await client.post(f"/api/agents/{agent['id']}/runs", json={"input": "question"})
        assert blocked.status_code == 422, blocked.text
        assert blocked.json()["detail"]["error"] == "missing_secrets"
        async with async_session_factory() as session:
            assert (await session.execute(select(Run))).scalars().all() == []

        current = await _current(client, agent["id"])
        saved = await client.put(f"/api/agents/{agent['id']}/definition", json={
            "definition": current["definition"], "base_version_id": current["base_version_id"],
            "secret_inputs": {"required-key": "now-provided"},
        })
        assert saved.status_code == 200, saved.text
        run = await client.post(f"/api/agents/{agent['id']}/runs", json={"input": "question"})
        assert run.status_code == 200, run.text
        assert run.json()["output"] == "secret-answer"
        assert "now-provided" not in run.text


@pytest.mark.asyncio
async def test_definition_save_inherits_secret_and_rejects_stale_base_without_overwrite() -> None:
    agent = await _create_agent("optimistic", config={
        "nested": {"api_key": "original-current-secret"},
        "mock_replies": {"question": "answer"},
    })
    async with _client() as client:
        current = await _current(client, agent["id"])
        first_definition = copy.deepcopy(current["definition"])
        first_definition["description"] = "first-writer"
        first = await client.put(f"/api/agents/{agent['id']}/definition", json={
            "definition": first_definition, "base_version_id": current["base_version_id"],
        })
        assert first.status_code == 200, first.text
        assert first.json()["version_number"] == 2
        assert first.json()["inherited_secrets"] == [{
            "path": "/config/nested/api_key", "ref": "nested.api_key", "required": False,
        }]
        assert "original-current-secret" not in first.text

        stale_definition = copy.deepcopy(first_definition)
        stale_definition["description"] = "stale-writer"
        stale = await client.put(f"/api/agents/{agent['id']}/definition", json={
            "definition": stale_definition, "base_version_id": current["base_version_id"],
        })
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["error"] == "stale_base_version"
        assert stale.json()["detail"]["latest_version_id"] == first.json()["latest_version_id"]
        agent_after = (await client.get(f"/api/agents/{agent['id']}")).json()
        assert agent_after["description"] == "first-writer"
        versions = (await client.get(f"/api/agents/{agent['id']}/versions")).json()
        assert len(versions) == 2
        async with async_session_factory() as session:
            stored = await session.get(Agent, agent["id"])
            assert reveal_config(json.loads(stored.config))["nested"]["api_key"] == "original-current-secret"


@pytest.mark.asyncio
async def test_canonical_knowledge_mapping_is_explicit_and_snapshot_preserves_ref() -> None:
    async with _client() as client:
        kb_response = await client.post("/api/knowledge-bases", json={"name": "target-private-kb"})
        assert kb_response.status_code == 201, kb_response.text
        kb_id = kb_response.json()["id"]
        row = _definition(
            "mapped-kb",
            resources={"prompt": None, "knowledge_bases": [{"ref": "kb-来源/1", "name": "source-name"}]},
        )
        options = {"resource_map": json.dumps({"knowledge_bases": {"kb-来源/1": kb_id}})}
        preflight = await client.post(
            "/api/agents/import?dry_run=true", files=_files(_package(row)), data=options,
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["resolved"] == [{
            "agent": "mapped-kb", "kind": "knowledge_base", "ref": "kb-来源/1", "target_id": kb_id,
        }]
        assert (await client.get("/api/agents")).json() == []
        imported = await client.post("/api/agents/import", files=_files(_package(row)), data=options)
        assert imported.status_code == 200, imported.text
        agent = (await client.get("/api/agents")).json()[0]
        current = await _current(client, agent["id"])
        assert current["definition"]["resources"]["knowledge_bases"] == [{
            "ref": "kb-来源/1", "name": "target-private-kb",
        }]
        assert "knowledge_base_ids" not in current["definition"]["config"]
        async with async_session_factory() as session:
            bindings = (await session.execute(select(KnowledgeBinding))).scalars().all()
            assert [(item.agent_id, item.kb_id) for item in bindings] == [(agent["id"], kb_id)]
            version = await session.get(AgentVersion, current["latest_version_id"])
            resources = json.loads(version.snapshot)["resources"]["knowledge_bases"]
            assert resources == [{"kb_id": kb_id, "ref": "kb-来源/1", "name": "target-private-kb"}]
        exported = await client.post("/api/agents/export", json={"agent_ids": [agent["id"]]})
        assert kb_id not in exported.text
        assert exported.json()["agents"][0]["resources"]["knowledge_bases"][0]["ref"] == "kb-来源/1"


@pytest.mark.asyncio
async def test_unresolved_resource_aborts_whole_batch_and_cross_team_id_is_not_accepted() -> None:
    async with async_session_factory() as session:
        session.add(KnowledgeBase(id="other-team-kb", team_id="other-team", name="private-other-name"))
        await session.commit()
    valid = _definition("would-be-created")
    unresolved = _definition(
        "unresolved-target",
        resources={"prompt": None, "knowledge_bases": [{"ref": "kb-1", "name": "private-other-name"}]},
    )
    mapping = {"resource_map": json.dumps({"knowledge_bases": {"kb-1": "other-team-kb"}})}
    async with _client() as client:
        dry_run = await client.post(
            "/api/agents/import?dry_run=true", files=_files(_package(valid, unresolved)), data=mapping,
        )
        assert dry_run.status_code == 200, dry_run.text
        assert dry_run.json()["ok"] is False
        assert dry_run.json()["unresolved"] == [{
            "agent": "unresolved-target", "kind": "knowledge_base", "ref": "kb-1",
            "reason": "resource_unavailable",
        }]
        assert "private-other-name" not in dry_run.text
        assert (await client.get("/api/agents")).json() == []
        imported = await client.post(
            "/api/agents/import", files=_files(_package(valid, unresolved)), data=mapping,
        )
        assert imported.status_code == 422, imported.text
        assert imported.json()["detail"]["error"] == "resource_preflight_failed"
        assert (await client.get("/api/agents")).json() == []
        async with async_session_factory() as session:
            assert (await session.execute(select(AgentVersion))).scalars().all() == []
            assert (await session.execute(select(KnowledgeBinding))).scalars().all() == []


@pytest.mark.asyncio
async def test_new_agent_prompt_mapping_copies_owned_versions_and_pins_content() -> None:
    source = await _create_agent("prompt-source")
    async with _client() as client:
        template_response = await client.post(f"/api/agents/{source['id']}/prompt-templates", json={
            "name": "research-system", "template": "Evidence for {audience}", "description": "source",
        })
        assert template_response.status_code == 201, template_response.text
        source_template_id = template_response.json()["id"]
        row = _definition(
            "prompt-target",
            config={"prompt_variables": {"audience": "开发者"}},
            resources={"prompt": {"name": "research-system", "version": 1}, "knowledge_bases": []},
        )
        mapping = {"resource_map": json.dumps({"prompts": {"research-system": source_template_id}})}
        preflight = await client.post(
            "/api/agents/import?dry_run=true", files=_files(_package(row)), data=mapping,
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["resolved"][0]["action"] == "copy"
        assert len((await client.get("/api/agents")).json()) == 1
        imported = await client.post("/api/agents/import", files=_files(_package(row)), data=mapping)
        assert imported.status_code == 200, imported.text
        agents = (await client.get("/api/agents")).json()
        target = next(item for item in agents if item["name"] == "prompt-target")
        current = await _current(client, target["id"])
        assert current["definition"]["resources"]["prompt"] == {"name": "research-system", "version": 1}
        current_text = json.dumps(current, ensure_ascii=False)
        assert "template_id" not in current_text and "content_sha256" not in current_text
        async with async_session_factory() as session:
            stored = await session.get(Agent, target["id"])
            target_template_id = json.loads(stored.config)["prompt_template_id"]
            assert target_template_id != source_template_id
            copied = await session.get(PromptTemplate, target_template_id)
            original = await session.get(PromptTemplate, source_template_id)
            assert copied.agent_id == target["id"] and original.agent_id == source["id"]
            snapshot = json.loads((await session.get(AgentVersion, current["latest_version_id"])).snapshot)
            prompt = snapshot["resources"]["prompt"]
            assert prompt["template_id"] == target_template_id
            assert prompt["content"] == "Evidence for {audience}"
            assert prompt["content_sha256"] == hashlib.sha256(b"Evidence for {audience}").hexdigest()


@pytest.mark.asyncio
async def test_invalid_canonical_entry_rejects_whole_batch_without_echoing_secret() -> None:
    valid = _definition("valid-before-invalid")
    invalid = _definition("invalid-secret")
    invalid["config"]["api_key"] = "must-not-be-echoed"
    invalid["secret_refs"] = [{"path": "/config/api_key", "ref": "key", "required": False}]
    async with _client() as client:
        response = await client.post("/api/agents/import", files=_files(_package(valid, invalid)))
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["error"] == "invalid_definition"
        assert "must-not-be-echoed" not in response.text
        assert (await client.get("/api/agents")).json() == []

        duplicate = _definition("duplicate-tools", tools=["echo", "echo"])
        duplicate_response = await client.post("/api/agents/import", files=_files(_package(duplicate)))
        assert duplicate_response.status_code == 422
        assert (await client.get("/api/agents")).json() == []

        future = _definition("future-schema")
        future["schema_version"] = 2
        future_response = await client.post("/api/agents/import", files=_files(_package(future)))
        assert future_response.status_code == 422
        assert (await client.get("/api/agents")).json() == []

        duplicate_name = await client.post("/api/agents/import", files=_files(_package(valid, valid)))
        assert duplicate_name.status_code == 422
        assert (await client.get("/api/agents")).json() == []


@pytest.mark.asyncio
async def test_current_definition_can_add_knowledge_without_changing_existing_ref() -> None:
    agent = await _create_agent("editable-resources")
    async with _client() as client:
        first_kb = (await client.post("/api/knowledge-bases", json={"name": "kb-first"})).json()
        second_kb = (await client.post("/api/knowledge-bases", json={"name": "kb-second"})).json()
        bound = await client.put(f"/api/agents/{agent['id']}/knowledge/{first_kb['id']}")
        assert bound.status_code == 201, bound.text
        current = await _current(client, agent["id"])
        definition = current["definition"]
        original = definition["resources"]["knowledge_bases"][0]
        assert original["ref"] == "kb-1"
        definition["resources"]["knowledge_bases"].append({"ref": "kb-custom", "name": "source-second"})
        saved = await client.put(f"/api/agents/{agent['id']}/definition", json={
            "definition": definition, "base_version_id": current["base_version_id"],
            "resource_map": {"knowledge_bases": {"kb-custom": second_kb["id"]}},
        })
        assert saved.status_code == 200, saved.text
        resources = saved.json()["definition"]["resources"]["knowledge_bases"]
        assert resources == [
            {"ref": "kb-1", "name": "kb-first"}, {"ref": "kb-custom", "name": "kb-second"},
        ]
        async with async_session_factory() as session:
            bindings = (await session.execute(select(KnowledgeBinding))).scalars().all()
            assert {row.kb_id for row in bindings} == {first_kb["id"], second_kb["id"]}
            snapshot = json.loads((await session.get(AgentVersion, saved.json()["latest_version_id"])).snapshot)
            assert [row["ref"] for row in snapshot["resources"]["knowledge_bases"]] == ["kb-1", "kb-custom"]


@pytest.mark.asyncio
async def test_canonical_masked_overwrite_reports_inheritance_and_preserves_secret() -> None:
    agent = await _create_agent("canonical-mask", config={
        "nested": {"api_key": "masked-original"}, "mock_replies": {"question": "answer"},
    })
    async with _client() as client:
        current = await _current(client, agent["id"])
        definition = current["definition"]
        definition["config"]["nested"]["api_key"] = "********"
        files = _files(_package(definition))
        preflight = await client.post(
            "/api/agents/import?dry_run=true", data={"on_conflict": "overwrite"}, files=files,
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["inherited_secrets"] == [{
            "agent": "canonical-mask", "path": "/config/nested/api_key",
            "ref": "nested.api_key", "required": False,
        }]
        imported = await client.post(
            "/api/agents/import", data={"on_conflict": "overwrite"}, files=files,
        )
        assert imported.status_code == 200, imported.text
        async with async_session_factory() as session:
            stored = await session.get(Agent, agent["id"])
            assert reveal_config(json.loads(stored.config))["nested"]["api_key"] == "masked-original"


@pytest.mark.asyncio
async def test_second_write_failure_rolls_back_first_agent_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent.imports as import_service

    original = import_service.create_agent_version
    calls = 0

    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("simulated second-write failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(import_service, "create_agent_version", fail_second)
    async with _client() as client:
        response = await client.post(
            "/api/agents/import", files=_files(_package(_definition("first-write"), _definition("second-write"))),
        )
        assert response.status_code == 422, response.text
        assert (await client.get("/api/agents")).json() == []
        async with async_session_factory() as session:
            assert (await session.execute(select(AgentVersion))).scalars().all() == []


@pytest.mark.asyncio
async def test_canonical_yaml_and_unsupported_tool_preflight() -> None:
    row = _definition("yaml-研究", extensions={"unicode": "保留"})
    content = yaml.safe_dump(
        {"version": 2, "kind": "agentforge.agent", "agents": [row]},
        allow_unicode=True, sort_keys=False,
    ).encode("utf-8")
    async with _client() as client:
        imported = await client.post("/api/agents/import", files=_files(content, "agents.yaml"))
        assert imported.status_code == 200, imported.text
        assert imported.json()["format"] == "yaml"
        unsupported = _definition("unknown-tool", tools=["not-a-real-tool"])
        preflight = await client.post(
            "/api/agents/import?dry_run=true", files=_files(_package(unsupported)),
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["ok"] is False
        assert preflight.json()["unsupported_tools"] == [{
            "agent": "unknown-tool", "tool": "not-a-real-tool",
        }]
        assert [item["name"] for item in (await client.get("/api/agents")).json()] == ["yaml-研究"]


@pytest.mark.asyncio
async def test_multi_agent_export_namespaces_duplicate_refs_for_independent_mapping() -> None:
    agent_a = await _create_agent("package-a", config={
        "nested": {"api_key": "secret-a"}, "mock_replies": {"question": "answer-a"},
    })
    agent_b = await _create_agent("package-b", config={
        "nested": {"api_key": "secret-b"}, "mock_replies": {"question": "answer-b"},
    })
    async with _client() as client:
        kb_a = (await client.post("/api/knowledge-bases", json={"name": "package-kb-a"})).json()
        kb_b = (await client.post("/api/knowledge-bases", json={"name": "package-kb-b"})).json()
        assert (await client.put(f"/api/agents/{agent_a['id']}/knowledge/{kb_a['id']}")).status_code == 201
        assert (await client.put(f"/api/agents/{agent_b['id']}/knowledge/{kb_b['id']}")).status_code == 201
        exported = await client.post(
            "/api/agents/export", json={"agent_ids": [agent_a["id"], agent_b["id"]]},
        )
        assert exported.status_code == 200, exported.text
        rows = exported.json()["agents"]
        by_original_name = {row["name"]: row for row in rows}
        assert by_original_name["package-a"]["resources"]["knowledge_bases"][0]["ref"] == "package-a:kb-1"
        assert by_original_name["package-b"]["resources"]["knowledge_bases"][0]["ref"] == "package-b:kb-1"
        assert by_original_name["package-a"]["secret_refs"][0]["ref"] == "package-a:nested.api_key"
        assert by_original_name["package-b"]["secret_refs"][0]["ref"] == "package-b:nested.api_key"

        kb_targets = {"package-a": kb_a["id"], "package-b": kb_b["id"]}
        secret_inputs = {}
        resource_map = {"knowledge_bases": {}, "prompts": {}}
        for original_name, row in by_original_name.items():
            kb_ref = row["resources"]["knowledge_bases"][0]["ref"]
            secret_ref = row["secret_refs"][0]["ref"]
            resource_map["knowledge_bases"][kb_ref] = kb_targets[original_name]
            secret_inputs[secret_ref] = f"new-secret-{original_name}"
            row["name"] = f"imported-{original_name}"
        options = {
            "resource_map": json.dumps(resource_map),
            "secret_inputs": json.dumps(secret_inputs),
        }
        preflight = await client.post(
            "/api/agents/import?dry_run=true", files=_files(_package(*rows)), data=options,
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["ok"] is True, preflight.text
        imported = await client.post("/api/agents/import", files=_files(_package(*rows)), data=options)
        assert imported.status_code == 200, imported.text
        assert "new-secret-package" not in imported.text
        async with async_session_factory() as session:
            for original_name in ("package-a", "package-b"):
                imported_name = f"imported-{original_name}"
                stored = (await session.execute(select(Agent).where(Agent.name == imported_name))).scalar_one()
                config = reveal_config(json.loads(stored.config))
                assert config["nested"]["api_key"] == f"new-secret-{original_name}"
                binding = (await session.execute(
                    select(KnowledgeBinding).where(KnowledgeBinding.agent_id == stored.id)
                )).scalar_one()
                assert binding.kb_id == kb_targets[original_name]

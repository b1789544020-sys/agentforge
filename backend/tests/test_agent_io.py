"""Tests for the agent import / export file-based endpoints (0.1.2 #7)."""
from __future__ import annotations

import io
import json
import os
import uuid

os.environ.setdefault("AGENTFORGE_LLM_PROVIDER", "mock")

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from app.agent.exporter import (  # noqa: E402
    AGENT_FIELDS,
    FORMAT_VERSION,
    KIND,
    build_payload,
    dump as dump_payload,
)
from app.main import create_app  # noqa: E402


def _make_agent(client: AsyncClient, **overrides) -> dict:
    body = {
        "name": "demo",
        "description": "test",
        "system_prompt": "you are helpful",
        "model": "gpt-4o-mini",
        "provider": "openai",
        "temperature": 0.5,
        "max_steps": 8,
        "tools": ["search_knowledge"],
        "config": {"k": 1},
    }
    body.update(overrides)
    return body


async def _create_agent(client: AsyncClient, name: str, **overrides) -> str:
    # Use a unique name suffix so the test is order-independent
    # (the global in-memory DB is shared across the whole test
    # session; the schema autouse fixture does not drop rows between
    # tests, only on file-backed DBs).
    unique_name = f"{name}-{uuid.uuid4().hex[:8]}"
    body = _make_agent(client, name=unique_name, **overrides)
    resp = await client.post("/api/v1/agents", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _agent_stub(name: str = "x", description: str = "d", tools=None, config=None):
    if tools is None:
        tools = []
    if config is None:
        config = {}
    return type(
        "A",
        (),
        {
            "name": name,
            "description": description,
            "system_prompt": "p",
            "model": "gpt-4o-mini",
            "provider": "openai",
            "temperature": 0.7,
            "max_steps": 20,
            "tools": json.dumps(tools),
            "config": json.dumps(config),
        },
    )()


@pytest.mark.asyncio
async def test_export_all_agents_json_default() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _create_agent(client, "alpha")
        await _create_agent(client, "beta")
        resp = await client.post("/api/v1/agents/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert "attachment" in resp.headers.get("content-disposition", "")
        payload = resp.json()
        assert payload["version"] == FORMAT_VERSION
        assert payload["kind"] == KIND
        names = {a["name"] for a in payload["agents"]}
        # Two agents we just created must be present (alongside any
        # other test-leftover rows in the shared in-memory DB).
        assert any(n.startswith("alpha-") for n in names)
        assert any(n.startswith("beta-") for n in names)


@pytest.mark.asyncio
async def test_export_specific_ids_yaml() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        id_a = await _create_agent(client, "alpha", tools=["shell"])
        await _create_agent(client, "beta")
        resp = await client.post(
            "/api/v1/agents/export",
            params={"fmt": "yaml"},
            json={"agent_ids": [id_a]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("application/x-yaml")
        data = yaml.safe_load(resp.text)
        assert data["kind"] == KIND
        # We export a specific id, so we should get exactly one row back.
        assert len(data["agents"]) == 1
        assert data["agents"][0]["name"].startswith("alpha-")
        assert data["agents"][0]["tools"] == ["shell"]


@pytest.mark.asyncio
async def test_export_missing_id_404() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/agents/export",
            json={"agent_ids": ["does-not-exist"]},
        )
        assert resp.status_code == 404
        assert "missing_ids" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_export_unsupported_format_400() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/agents/export?fmt=xml")
        assert resp.status_code == 400
        assert "json" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_import_round_trip_json() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        name = f"rt-json-{uuid.uuid4().hex[:8]}"
        payload = build_payload(
            [_agent_stub(name=name, tools=["shell"], config={"x": 1})]
        )
        text = dump_payload(payload, "json")
        resp = await client.post(
            "/api/v1/agents/import",
            files={"file": ("agents.json", io.BytesIO(text.encode()), "application/json")},
            data={"on_conflict": "skip"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["imported"] == 1
        assert body["format"] == "json"
        assert body["created"] == [name]

        resp = await client.get("/api/v1/agents")
        names = {a["name"] for a in resp.json()}
        assert name in names


@pytest.mark.asyncio
async def test_import_round_trip_yaml() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        name = f"rt-yaml-{uuid.uuid4().hex[:8]}"
        agent_dict = {f: None for f in AGENT_FIELDS}
        agent_dict.update(
            {
                "name": name,
                "description": "d",
                "system_prompt": "p",
                "model": "gpt-4o-mini",
                "provider": "openai",
                "temperature": 0.4,
                "max_steps": 7,
                "tools": ["echo"],
                "config": {"k": 2},
            }
        )
        payload = {"version": FORMAT_VERSION, "kind": KIND, "agents": [agent_dict]}
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        resp = await client.post(
            "/api/v1/agents/import",
            files={"file": ("agents.yaml", io.BytesIO(text.encode()), "application/x-yaml")},
            data={"on_conflict": "skip"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["imported"] == 1
        assert body["format"] == "yaml"
        assert body["created"] == [name]


@pytest.mark.asyncio
async def test_import_skip_existing() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create a baseline agent in the DB.
        existing_id = await _create_agent(client, "exists", description="original")
        # Look up the actual name that was created (it has a uuid suffix).
        resp = await client.get(f"/api/v1/agents/{existing_id}")
        existing_name = resp.json()["name"]
        # Now import the same name with different description.
        payload = build_payload(
            [_agent_stub(name=existing_name, description="should-be-ignored")]
        )
        text = dump_payload(payload, "json")
        resp = await client.post(
            "/api/v1/agents/import",
            files={"file": ("x.json", io.BytesIO(text.encode()), "application/json")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["imported"] == 0
        assert body["skipped"] == 1
        assert body["skipped_list"] == [existing_name]

        # Original record untouched.
        resp = await client.get(f"/api/v1/agents/{existing_id}")
        assert resp.json()["description"] == "original"


@pytest.mark.asyncio
async def test_import_overwrite_existing() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        existing_id = await _create_agent(client, "victim", description="old")
        resp = await client.get(f"/api/v1/agents/{existing_id}")
        existing_name = resp.json()["name"]
        # Build a different stub with overwritten fields.
        overwrite = type(
            "A",
            (),
            {
                "name": existing_name,
                "description": "new",
                "system_prompt": "p2",
                "model": "claude-3-haiku",
                "provider": "anthropic",
                "temperature": 0.1,
                "max_steps": 4,
                "tools": json.dumps(["shell"]),
                "config": json.dumps({"a": 1}),
            },
        )()
        payload = build_payload([overwrite])
        text = dump_payload(payload, "json")
        resp = await client.post(
            "/api/v1/agents/import",
            files={"file": ("x.json", io.BytesIO(text.encode()), "application/json")},
            data={"on_conflict": "overwrite"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["updated"] == 1
        assert body["updated_list"] == [existing_name]

        resp = await client.get(f"/api/v1/agents/{existing_id}")
        match = resp.json()
        assert match["description"] == "new"
        assert match["model"] == "claude-3-haiku"
        assert match["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_import_error_on_conflict_aborts() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        existing_id = await _create_agent(client, "dup")
        resp = await client.get(f"/api/v1/agents/{existing_id}")
        existing_name = resp.json()["name"]
        new_name = f"never-imported-{uuid.uuid4().hex[:8]}"
        payload = build_payload(
            [
                _agent_stub(name=existing_name),
                _agent_stub(name=new_name),
            ]
        )
        text = dump_payload(payload, "json")
        resp = await client.post(
            "/api/v1/agents/import",
            files={"file": ("x.json", io.BytesIO(text.encode()), "application/json")},
            data={"on_conflict": "error"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["name"] == existing_name

        # The second agent should NOT have been imported.
        resp = await client.get("/api/v1/agents")
        names = {a["name"] for a in resp.json()}
        assert new_name not in names


@pytest.mark.asyncio
async def test_import_invalid_version_400() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        bad = json.dumps({"version": 999, "kind": KIND, "agents": []})
        resp = await client.post(
            "/api/v1/agents/import",
            files={"file": ("x.json", io.BytesIO(bad.encode()), "application/json")},
        )
        assert resp.status_code == 400
        assert "version" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_import_invalid_kind_400() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        bad = json.dumps({"version": FORMAT_VERSION, "kind": "other.kind", "agents": []})
        resp = await client.post(
            "/api/v1/agents/import",
            files={"file": ("x.json", io.BytesIO(bad.encode()), "application/json")},
        )
        assert resp.status_code == 400
        assert "kind" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_export_then_import_is_idempotent_on_skip() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        existing_id = await _create_agent(client, "round", tools=["shell"], temperature=0.42)
        # Export only our agent (so the result is a single, well-known row).
        resp = await client.post(
            "/api/v1/agents/export",
            json={"agent_ids": [existing_id]},
        )
        assert resp.status_code == 200, resp.text
        text = resp.text
        # Re-import the same blob — should skip the only row.
        resp = await client.post(
            "/api/v1/agents/import",
            files={"file": ("agents.json", io.BytesIO(text.encode()), "application/json")},
            data={"on_conflict": "skip"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["imported"] == 0
        assert body["skipped"] == 1
        assert body["updated"] == 0


@pytest.mark.asyncio
async def test_import_detects_format_from_filename() -> None:
    """The format detection should fall back to the file extension."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {"version": FORMAT_VERSION, "kind": KIND, "agents": []}
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        resp = await client.post(
            "/api/v1/agents/import",
            files={"file": ("agents.yml", io.BytesIO(text.encode()), "application/x-yaml")},
        )
        assert resp.status_code == 200
        assert resp.json()["format"] == "yaml"


# ---------------------------------------------------------------------------
# Pure-function unit tests (no DB, no FastAPI)
# ---------------------------------------------------------------------------


def test_exporter_pure_round_trip_json() -> None:
    from app.agent.exporter import load as exp_load

    agent_dict = {f: f"v-{f}" for f in AGENT_FIELDS}
    agent_dict["name"] = "pure"
    agent_dict["tools"] = ["a", "b"]
    agent_dict["config"] = {"k": 7}
    payload = {"version": FORMAT_VERSION, "kind": KIND, "agents": [agent_dict]}
    text = dump_payload(payload, "json")
    back = exp_load(text, "json")
    rows = agents_from_payload_helper(back)
    assert rows[0]["name"] == "pure"
    assert rows[0]["tools"] == ["a", "b"]
    assert rows[0]["config"] == {"k": 7}


def test_exporter_pure_round_trip_yaml() -> None:
    from app.agent.exporter import load as exp_load

    agent_dict = {f: f"v-{f}" for f in AGENT_FIELDS}
    agent_dict["name"] = "pure-y"
    agent_dict["tools"] = ["a"]
    agent_dict["config"] = {"y": 1}
    payload = {"version": FORMAT_VERSION, "kind": KIND, "agents": [agent_dict]}
    text = dump_payload(payload, "yaml")
    back = exp_load(text, "yaml")
    rows = agents_from_payload_helper(back)
    assert rows[0]["name"] == "pure-y"
    assert rows[0]["tools"] == ["a"]


def test_exporter_drops_unknown_fields() -> None:
    """Forward-compatible imports: extra keys are silently dropped."""
    from app.agent.exporter import load as exp_load

    payload = {
        "version": FORMAT_VERSION,
        "kind": KIND,
        "agents": [
            {
                "name": "future",
                "description": "d",
                "system_prompt": "",
                "model": "x",
                "provider": "y",
                "temperature": 0.0,
                "max_steps": 1,
                "tools": [],
                "config": {},
                "new_in_v2": "should be dropped",
            }
        ],
    }
    text = dump_payload(payload, "json")
    rows = agents_from_payload_helper(exp_load(text, "json"))
    assert "new_in_v2" not in rows[0]


def test_exporter_rejects_garbage_version() -> None:
    from app.agent.exporter import load as exp_load

    with pytest.raises(ValueError):
        exp_load(json.dumps({"version": 0, "kind": KIND, "agents": []}), "json")
    with pytest.raises(ValueError):
        exp_load(
            json.dumps({"version": FORMAT_VERSION, "kind": "wrong", "agents": []}),
            "json",
        )


def test_exporter_rejects_missing_agents_list() -> None:
    from app.agent.exporter import load as exp_load

    with pytest.raises(ValueError):
        exp_load(json.dumps({"version": FORMAT_VERSION, "kind": KIND}), "json")


def test_exporter_rejects_missing_name() -> None:
    from app.agent.exporter import load as exp_load

    bad = {"version": FORMAT_VERSION, "kind": KIND, "agents": [{"description": "no name"}]}
    with pytest.raises(ValueError):
        agents_from_payload_helper(exp_load(json.dumps(bad), "json"))


def agents_from_payload_helper(payload):
    from app.agent.exporter import agents_from_payload

    return agents_from_payload(payload)




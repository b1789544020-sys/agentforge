"""Integration tests for persisted four-layer Run and Gate evidence."""
from __future__ import annotations

import uuid

import pytest
from app.database import async_session_factory
from app.knowledge import index_document
from app.main import create_app
from app.models import EvalCase, KnowledgeBase, KnowledgeDocument, Run
from httpx import ASGITransport, AsyncClient


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def _agent(client: AsyncClient, replies: dict[str, str] | None = None, **overrides) -> str:
    payload = {
        "name": f"quality-{uuid.uuid4().hex[:8]}", "provider": "mock", "model": "mock-v1",
        "system_prompt": "test", "temperature": 0, "max_steps": 3, "tools": [],
        "config": {"mock_replies": replies or {}}, **overrides,
    }
    response = await client.post("/api/agents", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _suite(client: AsyncClient, agent_id: str, case_ids: list[str]) -> dict:
    response = await client.post("/api/suites", json={
        "name": "quality-evidence", "agent_id": agent_id, "case_ids": case_ids,
        "pass_threshold": 1.0, "scorer": "composite", "auto_on_save": False,
    })
    assert response.status_code == 201, response.text
    return response.json()


async def _gate(client: AsyncClient, agent_id: str, version_id: str, suite_id: str) -> dict:
    response = await client.post(f"/api/agents/{agent_id}/gates", json={
        "agent_version_id": version_id, "suite_id": suite_id,
    })
    assert response.status_code == 202, response.text
    detail = await client.get(f"/api/agents/{agent_id}/gates/{response.json()['id']}")
    assert detail.status_code == 200, detail.text
    return detail.json()


async def _cases(inputs: list[str]) -> list[str]:
    ids = [str(uuid.uuid4()) for _ in inputs]
    async with async_session_factory() as session:
        for index, (case_id, text) in enumerate(zip(ids, inputs, strict=True)):
            session.add(EvalCase(
                id=case_id, dataset="quality-evidence", name=text, input=text, expected="right",
                meta="{}", index=index, tool_calls="[]", score_details="{}",
            ))
        await session.commit()
    return ids


@pytest.mark.asyncio
async def test_run_detail_has_real_retrieval_citation_and_legacy_summary() -> None:
    async with _client() as client:
        agent_id = await _agent(client, {"Alpha": "Alpha is grounded [1]."})
        kb_id = str(uuid.uuid4())
        async with async_session_factory() as session:
            session.add(KnowledgeBase(id=kb_id, name="public-test"))
            document = KnowledgeDocument(
                id=str(uuid.uuid4()), kb_id=kb_id, filename="facts.md",
                media_type="text/markdown", content="# Facts\n\nAlpha is grounded.", status="ready",
            )
            session.add(document)
            await session.flush()
            await index_document(session, document)
            await session.commit()
        binding = await client.put(f"/api/agents/{agent_id}/knowledge/{kb_id}")
        assert binding.status_code == 201, binding.text
        response = await client.post(f"/api/agents/{agent_id}/runs", json={"input": "Alpha"})
        assert response.status_code == 200, response.text
        run_id = response.json()["id"]
        detail = (await client.get(f"/api/agents/{agent_id}/runs/{run_id}")).json()
        assert detail["quality_record_version"] == 1
        assert detail["answer_output"]["evidence"], detail
        assert detail["answer_output"]["evidence"][0]["citation_text"] == "[1]"
        assert detail["retrieval_output"]["sources"][0]["locator"] == {
            "filename": "facts.md", "section": "Facts",
        }
        assert detail["execution_output"]["result"]["record_complete"] is True
        assert detail["tool_output"] == {"calls": [], "results": []}
        summary = (await client.get(f"/api/agents/{agent_id}/runs")).json()[0]
        assert "answer_output" not in summary and "execution_output" not in summary


@pytest.mark.asyncio
async def test_tool_observation_and_legacy_run_nulls() -> None:
    async with _client() as client:
        agent_id = await _agent(client, tools=["calculator"])
        response = await client.post(f"/api/agents/{agent_id}/runs", json={"input": "2+2"})
        assert response.status_code == 200, response.text
        detail = (await client.get(
            f"/api/agents/{agent_id}/runs/{response.json()['id']}"
        )).json()
        assert detail["tool_output"]["calls"][0]["name"] == "calculator"
        assert detail["tool_output"]["results"][0]["status"] == "success"
        assert detail["execution_output"]["metrics"]["tool_call_count"] == 1
        legacy_id = str(uuid.uuid4())
        async with async_session_factory() as session:
            session.add(Run(id=legacy_id, agent_id=agent_id, input="old", status="success"))
            await session.commit()
        legacy = (await client.get(f"/api/agents/{agent_id}/runs/{legacy_id}")).json()
        assert legacy["quality_record_version"] is None
        assert legacy["answer_output"] is None and legacy["execution_output"] is None


@pytest.mark.asyncio
async def test_gate_evidence_layer_diff_and_changed_conditions() -> None:
    case_ids = await _cases(["good", "bad"])
    good = {"good": "right", "bad": "right"}
    bad = {"good": "right", "bad": "wrong"}
    async with _client() as client:
        agent_id = await _agent(client, good)
        suite = await _suite(client, agent_id, case_ids)
        version1 = (await client.get(f"/api/agents/{agent_id}/versions")).json()[0]
        first = await _gate(client, agent_id, version1["id"], suite["id"])
        assert first["status"] == "passed" and first["quality_record_version"] == 1
        assert len(first["evidence"]) == 2
        assert all(item["layers"]["system"]["status"] == "passed" for item in first["evidence"])
        assert all(item["run_id"] for item in first["evidence"])

        response = await client.patch(f"/api/agents/{agent_id}", json={"config": {"mock_replies": bad}})
        assert response.status_code == 200, response.text
        version2 = (await client.get(f"/api/agents/{agent_id}/versions")).json()[0]
        second = await _gate(client, agent_id, version2["id"], suite["id"])
        assert second["status"] == "failed"
        bad_case_id = case_ids[1]
        regression = next(item for item in second["diff"] if item["case_id"] == bad_case_id)
        assert regression["change"] == "new_failure"
        assert regression["comparison_status"] == "comparable"
        assert regression["current_run_ids"] and regression["previous_run_ids"]
        failed = next(item for item in second["evidence"] if item["case_id"] == bad_case_id)
        assert failed["failed_layers"] == ["output"]
        assert failed["layers"]["system"]["status"] == "passed"

        response = await client.patch(f"/api/agents/{agent_id}", json={"config": {"mock_replies": good}})
        assert response.status_code == 200, response.text
        version3 = (await client.get(f"/api/agents/{agent_id}/versions")).json()[0]
        third = await _gate(client, agent_id, version3["id"], suite["id"])
        fixed = next(item for item in third["diff"] if item["case_id"] == bad_case_id)
        assert fixed["change"] == "fixed" and fixed["comparison_status"] == "comparable"
        output_change = next(item for item in fixed["layer_changes"] if item["layer"] == "output")
        assert output_change["change"] == "fixed"

        response = await client.patch(f"/api/agents/{agent_id}", json={"model": "mock-v2"})
        assert response.status_code == 200, response.text
        version4 = (await client.get(f"/api/agents/{agent_id}/versions")).json()[0]
        fourth = await _gate(client, agent_id, version4["id"], suite["id"])
        assert fourth["status"] == "passed"
        assert all(item["comparison_status"] == "conditions_changed" for item in fourth["diff"])
        assert all("/execution/model" in item["changed_conditions"] for item in fourth["diff"])
        assert all(
            layer["change"] == "not_comparable"
            for item in fourth["diff"] for layer in item["layer_changes"]
        )

"""Exact source-version and append-only human feedback resolution workflow."""
from __future__ import annotations

import uuid

import pytest
from app.database import async_session_factory
from app.main import create_app
from app.models import EvalCase, FeedbackResolution
from httpx import ASGITransport, AsyncClient


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def _gate(client: AsyncClient, agent_id: str, version_id: str, suite_id: str) -> dict:
    response = await client.post(f"/api/agents/{agent_id}/gates", json={
        "agent_version_id": version_id, "suite_id": suite_id,
    })
    assert response.status_code == 202, response.text
    return (await client.get(f"/api/agents/{agent_id}/gates/{response.json()['id']}")).json()


@pytest.mark.asyncio
async def test_feedback_reuses_gate_case_and_requires_exact_fixed_run() -> None:
    case_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        session.add(EvalCase(
            id=case_id, dataset="feedback-resolution", name="bad", input="bad", expected="right",
            meta="{}", tool_calls="[]", score_details="{}",
        ))
        await session.commit()
    async with _client() as client:
        created = await client.post("/api/agents", json={
            "name": "feedback-resolution", "provider": "mock", "model": "mock-v1",
            "system_prompt": "test", "temperature": 0, "tools": [],
            "config": {"mock_replies": {"bad": "right"}},
        })
        agent_id = created.json()["id"]
        suite_response = await client.post("/api/suites", json={
            "name": "feedback-resolution", "agent_id": agent_id, "case_ids": [case_id],
            "pass_threshold": 1.0, "scorer": "composite", "auto_on_save": False,
        })
        suite = suite_response.json()
        version1 = (await client.get(f"/api/agents/{agent_id}/versions")).json()[0]
        first = await _gate(client, agent_id, version1["id"], suite["id"])
        assert first["status"] == "passed"

        await client.patch(f"/api/agents/{agent_id}", json={
            "config": {"mock_replies": {"bad": "wrong"}},
        })
        version2 = (await client.get(f"/api/agents/{agent_id}/versions")).json()[0]
        second = await _gate(client, agent_id, version2["id"], suite["id"])
        assert second["status"] == "failed"
        source_run_id = second["evidence"][0]["run_id"]
        original_second_evidence = second["evidence"]
        marked = await client.post("/api/operations/feedback", json={
            "run_id": source_run_id, "rating": "negative", "expected_answer": "right",
        })
        assert marked.status_code == 201, marked.text
        feedback = marked.json()
        assert feedback["agent_version_id"] == version2["id"]
        converted = await client.post(
            f"/api/operations/feedback/{feedback['id']}/convert",
            json={"expected_answer": "right", "suite_id": suite["id"]},
        )
        assert converted.status_code == 201, converted.text
        assert converted.json()["eval_case_id"] == case_id
        current_suite = (await client.get(f"/api/suites/{suite['id']}")).json()
        assert current_suite["case_ids"] == [case_id]

        pending = await client.post(
            f"/api/operations/feedback/{feedback['id']}/resolutions",
            json={
                "gate_run_id": second["id"], "case_id": case_id,
                "verdict": "needs_review", "note": "保留失败基线，等待修复版本人工复核。",
            },
        )
        assert pending.status_code == 201, pending.text
        rejected = await client.post(
            f"/api/operations/feedback/{feedback['id']}/resolutions",
            json={
                "gate_run_id": second["id"], "case_id": case_id,
                "verdict": "confirmed_fixed", "note": "失败版本不能写成已修复。",
            },
        )
        assert rejected.status_code == 422

        await client.patch(f"/api/agents/{agent_id}", json={
            "config": {"mock_replies": {"bad": "right"}},
        })
        version3 = (await client.get(f"/api/agents/{agent_id}/versions")).json()[0]
        third = await _gate(client, agent_id, version3["id"], suite["id"])
        assert third["status"] == "passed"
        assert third["diff"][0]["change"] == "fixed"
        assert source_run_id in third["diff"][0]["previous_run_ids"]
        resolved = await client.post(
            f"/api/operations/feedback/{feedback['id']}/resolutions",
            json={
                "gate_run_id": third["id"], "case_id": case_id,
                "verdict": "confirmed_fixed", "note": "人工打开原 Run 与新 Run，必要答案和来源版本均已核对。",
            },
        )
        assert resolved.status_code == 201, resolved.text
        history = (await client.get(
            f"/api/operations/feedback/{feedback['id']}/resolutions"
        )).json()
        assert [item["verdict"] for item in history] == ["needs_review", "confirmed_fixed"]
        second_after = (await client.get(f"/api/agents/{agent_id}/gates/{second['id']}")).json()
        assert second_after["evidence"] == original_second_evidence
        async with async_session_factory() as session:
            stored = (await session.execute(
                FeedbackResolution.__table__.select().where(
                    FeedbackResolution.feedback_id == feedback["id"]
                )
            )).all()
            assert len(stored) == 2

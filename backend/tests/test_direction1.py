"""Direction 1: immutable versions, quality gates, diff, and policy tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import async_session_factory
from app.gates.service import build_case_diff, execute_due_gates
from app.main import create_app
from app.models import AgentVersion, EvalCase, EvalSuite, GateBypass, GateRun


def _answers(bad: bool = False) -> dict[str, str]:
    return {
        f"case-{index}": ("wrong" if bad and index <= 2 else f"answer-{index}")
        for index in range(1, 5)
    }


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def _cases() -> list[str]:
    ids: list[str] = []
    async with async_session_factory() as session:
        for index in range(1, 5):
            case_id = str(uuid.uuid4())
            ids.append(case_id)
            session.add(EvalCase(
                id=case_id, eval_run_id=None, dataset="gate-test", name=f"case-{index}",
                input=f"case-{index}", expected=f"answer-{index}", meta="{}", index=index - 1,
                tool_calls="[]", score_details="{}",
            ))
        await session.commit()
    return ids


async def _create_agent(client: AsyncClient, name: str = "gate-e2e") -> str:
    response = await client.post("/api/agents", json={
        "name": name, "system_prompt": "good prompt", "provider": "mock", "model": "mock-v1",
        "temperature": 0, "max_steps": 2, "tools": [], "config": {"mock_replies": _answers()},
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_suite(client: AsyncClient, agent_id: str, case_ids: list[str], **overrides) -> dict:
    payload = {
        "name": "release-regression", "agent_id": agent_id, "case_ids": case_ids,
        "pass_threshold": 0.75, "scorer": "composite", "auto_on_save": False,
        **overrides,
    }
    response = await client.post("/api/suites", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _trigger(client: AsyncClient, agent_id: str, version_id: str, suite_id: str) -> dict:
    response = await client.post(f"/api/agents/{agent_id}/gates", json={
        "agent_version_id": version_id, "suite_id": suite_id,
    })
    assert response.status_code == 202, response.text
    gate_id = response.json()["id"]
    response = await client.get(f"/api/agents/{agent_id}/gates/{gate_id}")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_three_saves_keep_immutable_snapshots() -> None:
    async with _client() as client:
        agent_id = await _create_agent(client, "version-history")
        first = (await client.get(f"/api/agents/{agent_id}/versions")).json()[0]
        for prompt in ("second prompt", "third prompt"):
            response = await client.patch(f"/api/agents/{agent_id}", json={"system_prompt": prompt})
            assert response.status_code == 200, response.text
        versions = (await client.get(f"/api/agents/{agent_id}/versions")).json()
        assert [row["version_number"] for row in versions] == [3, 2, 1]
        assert versions[-1]["snapshot"] == first["snapshot"]
        assert versions[-1]["system_prompt"] == "good prompt"
        assert versions[0]["snapshot"]["system_prompt"] == "third prompt"
        assert versions[0]["snapshot"]["config"]["mock_replies"] == _answers()

        async with async_session_factory() as session:
            row = await session.get(AgentVersion, first["id"])
            row.system_prompt = "tampered"
            with pytest.raises(ValueError, match="immutable"):
                await session.commit()


@pytest.mark.asyncio
async def test_gate_two_regressions_diff_threshold_and_bypass() -> None:
    case_ids = await _cases()
    async with _client() as client:
        agent_id = await _create_agent(client)
        suite = await _create_suite(client, agent_id, case_ids)
        version1 = (await client.get(f"/api/agents/{agent_id}/versions")).json()[0]
        good = await _trigger(client, agent_id, version1["id"], suite["id"])
        assert good["status"] == "passed", good
        assert good["pass_rate"] == 1.0

        response = await client.patch(f"/api/agents/{agent_id}", json={
            "system_prompt": "bad prompt", "config": {"mock_replies": _answers(bad=True)},
        })
        assert response.status_code == 200, response.text
        version2 = (await client.get(f"/api/agents/{agent_id}/versions")).json()[0]
        bad = await _trigger(client, agent_id, version2["id"], suite["id"])
        assert bad["status"] == "failed", bad
        assert bad["pass_rate"] == 0.5
        assert set(bad["failed_case_ids"]) == set(case_ids[:2])
        regressions = [row for row in bad["diff"] if row["change"] == "new_failure"]
        assert {row["case_id"] for row in regressions} == set(case_ids[:2])
        assert all("answer_diff" in row for row in bad["diff"])
        current = await client.get(f"/api/agents/{agent_id}/versions/{version2['id']}")
        assert current.json()["gate_status"] == "failed"

        changed = await client.patch(f"/api/suites/{suite['id']}", json={"pass_threshold": 0.5})
        assert changed.status_code == 200
        reevaluated = await client.post(f"/api/agents/{agent_id}/gates/{bad['id']}/reevaluate")
        assert reevaluated.json()["status"] == "passed"

        await client.patch(f"/api/suites/{suite['id']}", json={"pass_threshold": 0.75})
        await client.post(f"/api/agents/{agent_id}/gates/{bad['id']}/reevaluate")
        bypass = await client.post(
            f"/api/agents/{agent_id}/versions/{version2['id']}/bypass",
            json={"gate_run_id": bad["id"], "reason": "Emergency release approved by platform admin"},
        )
        assert bypass.status_code == 201, bypass.text
        assert bypass.json()["reason"].startswith("Emergency")
        current = await client.get(f"/api/agents/{agent_id}/versions/{version2['id']}")
        assert current.json()["gate_status"] == "bypassed"
        async with async_session_factory() as session:
            audits = (await session.execute(select(GateBypass).where(GateBypass.gate_run_id == bad["id"]))).scalars().all()
            assert len(audits) == 1
            assert audits[0].created_at is not None


@pytest.mark.asyncio
async def test_suite_copy_automatic_and_scheduled_triggers() -> None:
    case_ids = await _cases()
    async with _client() as client:
        agent_id = await _create_agent(client, "trigger-modes")
        suite = await _create_suite(client, agent_id, case_ids, auto_on_save=True)
        copied = await client.post(f"/api/suites/{suite['id']}/copy")
        assert copied.status_code == 201
        assert copied.json()["enabled"] is False

        response = await client.patch(f"/api/agents/{agent_id}", json={"system_prompt": "auto save"})
        assert response.status_code == 200, response.text
        gates = (await client.get(f"/api/agents/{agent_id}/gates", params={"suite_id": suite["id"]})).json()
        assert any(row["trigger_source"] == "save" and row["status"] == "passed" for row in gates)

        async with async_session_factory() as session:
            row = await session.get(EvalSuite, suite["id"])
            row.schedule_interval_seconds = 60
            row.next_run_at = datetime.utcnow() - timedelta(seconds=1)
            await session.commit()
        executed = await execute_due_gates()
        assert executed
        async with async_session_factory() as session:
            scheduled = await session.get(GateRun, executed[-1])
            assert scheduled.trigger_source == "scheduled"
            assert scheduled.status == "passed"


def test_case_diff_classifies_fixed_and_unchanged() -> None:
    previous = [
        {"case_id": "a", "passed": False, "output": "old"},
        {"case_id": "b", "passed": True, "output": "same"},
    ]
    current = [
        {"case_id": "a", "passed": True, "output": "new"},
        {"case_id": "b", "passed": True, "output": "same"},
    ]
    diff = build_case_diff(current, previous)
    assert [row["change"] for row in diff] == ["fixed", "unchanged"]
    assert "-old" in diff[0]["answer_diff"] and "+new" in diff[0]["answer_diff"]

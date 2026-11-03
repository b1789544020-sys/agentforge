"""Direction 4: metrics, versioned costs, feedback loop, and alert tests."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import async_session_factory
from app.main import create_app
from app.models import Agent, EvalCase, EvalSuite, Organization, Run, Team


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def _agent(client: AsyncClient, model: str) -> str:
    response = await client.post("/api/agents", json={
        "name": f"ops-{uuid.uuid4()}", "provider": "mock", "model": model,
        "system_prompt": "operations test", "max_steps": 2, "tools": [],
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _run(agent_id: str, model: str, started: datetime, status: str = "success") -> Run:
    return Run(
        id=str(uuid.uuid4()), agent_id=agent_id, input="operations question", output="answer",
        status=status, error="permission denied" if status == "failed" else None,
        config=json.dumps({"model": model}), prompt_tokens=1_000_000, completion_tokens=500_000,
        total_tokens=1_500_000, duration_ms=125, started_at=started,
    )


@pytest.mark.asyncio
async def test_metrics_and_historical_cost_are_exact() -> None:
    async with _client() as client:
        model = f"priced-{uuid.uuid4()}"
        agent_id = await _agent(client, model)
        now = datetime.utcnow()
        old_run, current_run = _run(agent_id, model, now - timedelta(days=2)), _run(agent_id, model, now, "failed")
        async with async_session_factory() as session:
            session.add_all([old_run, current_run])
            await session.commit()
        for effective, input_price, output_price in ((now - timedelta(days=3), 1, 2), (now - timedelta(days=1), 3, 4)):
            response = await client.post("/api/operations/prices", json={
                "provider": "mock", "model": model, "input_per_million": input_price,
                "output_per_million": output_price, "effective_from": effective.isoformat(),
            })
            assert response.status_code == 201, response.text
        metrics = await client.get("/api/operations/metrics", params={"agent_id": agent_id, "group": "day"})
        assert metrics.status_code == 200, metrics.text
        summary = metrics.json()["summary"]
        assert summary["requests"] == 2 and summary["failed"] == 1
        assert summary["failure_rate"] == 0.5 and summary["rejected"] == 1
        assert summary["total_tokens"] == 3_000_000
        assert len(metrics.json()["trends"]) == 2
        assert model in metrics.json()["dimensions"]["models"]
        cost = await client.get("/api/operations/cost", params={"agent_id": agent_id})
        assert cost.status_code == 200, cost.text
        # Old: 1*1 + .5*2 = 2. New: 1*3 + .5*4 = 5. Total = 7.
        assert cost.json()["total"] == pytest.approx(7.0)
        assert cost.json()["unpriced_tokens"] == 0
        assert cost.json()["top_agents"][0]["agent_id"] == agent_id


@pytest.mark.asyncio
async def test_negative_feedback_becomes_case_and_enters_suite() -> None:
    async with _client() as client:
        agent_id = await _agent(client, "feedback-model")
        run = await client.post(f"/api/agents/{agent_id}/runs", json={"input": "original user question"})
        assert run.status_code == 200, run.text
        case_id = str(uuid.uuid4())
        async with async_session_factory() as session:
            session.add(EvalCase(id=case_id, agent_id=agent_id, dataset="seed", name="seed",
                                 input="seed", expected="seed", meta="{}", tool_calls="[]", score_details="{}"))
            await session.commit()
        suite = await client.post("/api/suites", json={
            "name": "feedback-regression", "agent_id": agent_id, "case_ids": [case_id], "auto_on_save": False,
        })
        assert suite.status_code == 201, suite.text
        feedback = await client.post("/api/operations/feedback", json={
            "run_id": run.json()["id"], "rating": "negative", "expected_answer": "initial correction",
        })
        assert feedback.status_code == 201, feedback.text
        assert feedback.json()["context_snapshot"]["input"] == "original user question"
        assert feedback.json()["context_snapshot"]["agent_version_id"]
        converted = await client.post(f"/api/operations/feedback/{feedback.json()['id']}/convert", json={
            "expected_answer": "human approved answer", "suite_id": suite.json()["id"],
        })
        assert converted.status_code == 201, converted.text
        new_case_id = converted.json()["eval_case_id"]
        async with async_session_factory() as session:
            case = await session.get(EvalCase, new_case_id)
            assert case.expected == "human approved answer" and case.input == "original user question"
            updated_suite = await session.get(EvalSuite, suite.json()["id"])
            assert new_case_id in json.loads(updated_suite.case_ids)
            assert case.meta.find("agent_version_id") >= 0


@pytest.mark.asyncio
async def test_failure_rate_alert_creates_deduplicated_inapp_event() -> None:
    async with _client() as client:
        model = f"alert-{uuid.uuid4()}"
        agent_id = await _agent(client, model)
        async with async_session_factory() as session:
            org = Organization(id=str(uuid.uuid4()), name=f"alerts-{uuid.uuid4()}")
            team = Team(id=str(uuid.uuid4()), org_id=org.id, name="alert-isolation")
            agent = await session.get(Agent, agent_id)
            agent.team_id = team.id
            failed_run = _run(agent_id, model, datetime.utcnow(), "failed")
            failed_run.team_id = team.id
            session.add_all([org, team, failed_run])
            await session.commit()
        rule = await client.post("/api/operations/alerts/rules", json={
            "name": "release failures", "kind": "failure_rate", "threshold": 0.2,
            "team_id": team.id,
        })
        assert rule.status_code == 201, rule.text
        first = await client.post("/api/operations/alerts/evaluate")
        assert first.status_code == 200 and first.json()["created"] == 1, first.text
        second = await client.post("/api/operations/alerts/evaluate")
        assert second.json()["created"] == 0
        events = await client.get("/api/operations/alerts/events")
        matching = [row for row in events.json() if row["rule_id"] == rule.json()["id"]]
        assert len(matching) == 1 and matching[0]["status"] == "open"
        acknowledged = await client.post(f"/api/operations/alerts/events/{matching[0]['id']}/ack")
        assert acknowledged.json()["status"] == "acknowledged"

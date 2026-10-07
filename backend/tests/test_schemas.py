"""Tests for the per-case + progress schemas in ``app.api.schemas``."""
from __future__ import annotations

from datetime import datetime

from app.api.schemas import (
    AgentOut,
    EvalCaseOut,
    EvalCreate,
    EvalProgressOut,
    EvalRunOut,
    RunOut,
)
from app.eval.scorer import EvalScore


def test_eval_create_is_dataset_ref_when_ids_provided() -> None:
    p = EvalCreate(test_case_ids=["a", "b"], cases=[])
    assert p.is_dataset_ref is True


def test_eval_create_is_not_dataset_ref_when_only_inline_cases() -> None:
    p = EvalCreate(cases=[{"input": "hi", "expected": "hello"}], test_case_ids=[])
    assert p.is_dataset_ref is False


def test_eval_create_is_not_dataset_ref_when_empty() -> None:
    p = EvalCreate()
    assert p.is_dataset_ref is False


def test_eval_case_out_round_trips_tool_calls_and_details() -> None:
    c = EvalCaseOut(
        id="x",
        index=0,
        input="hi",
        expected="hi",
        output="hi",
        score=1.0,
        passed=True,
        duration_ms=10,
        tokens=3,
        tool_calls=[{"name": "echo", "args": {"x": 1}}],
        score_details={"similarity": 1.0, "components": []},
    )
    d = c.model_dump()
    assert d["tool_calls"] == [{"name": "echo", "args": {"x": 1}}]
    assert d["score_details"]["similarity"] == 1.0


def test_eval_run_out_includes_scorer_and_threshold() -> None:
    r = EvalRunOut(
        id="r",
        agent_id="a",
        dataset="t",
        scorer="composite",
        pass_threshold=0.7,
        total=2,
        passed=2,
        failed=0,
        average_score=1.0,
        duration_ms=12,
        status="completed",
    )
    assert r.scorer == "composite"
    assert r.pass_threshold == 0.7
    assert r.cases == []  # default


def test_eval_progress_out_defaults() -> None:
    p = EvalProgressOut(
        eval_run_id="r",
        status="running",
        total=10,
        passed=3,
        failed=1,
        done=4,
    )
    assert p.progress == 0.4
    assert p.started_at is None
    assert p.finished_at is None


def test_eval_progress_out_completed_forces_full_progress() -> None:
    p = EvalProgressOut(
        eval_run_id="r",
        status="completed",
        total=4,
        passed=3,
        failed=1,
        done=4,
    )
    assert p.progress == 1.0


# ---------------------------------------------------------------------------
# Smoke: the scorer dataclass stays JSON-serializable for API responses
# ---------------------------------------------------------------------------
def test_eval_score_round_trip_dict() -> None:
    e = EvalScore.from_score(0.8, note="ok")
    blob = {
        "score": e.score,
        "passed": e.passed,
        "details": e.details,
    }
    assert blob["score"] == 0.8
    assert blob["details"]["note"] == "ok"


# ---------------------------------------------------------------------------
# Minimal sanity: agent / run schemas still serialise.
# ---------------------------------------------------------------------------
def test_agent_and_run_schemas_serialise() -> None:
    a = AgentOut(
        id="a",
        name="n",
        model="gpt-4o-mini",
        provider="openai",
        temperature=0.5,
        max_steps=10,
    )
    assert a.model_dump()["name"] == "n"

    r = RunOut(
        id="r",
        agent_id="a",
        input="hi",
        output="hello",
        status="success",
        step_count=2,
    )
    d = r.model_dump()
    assert d["output"] == "hello"
    assert d["trace"] == []


def test_datetime_in_agent_out() -> None:
    now = datetime.utcnow()
    a = AgentOut(
        id="a",
        name="n",
        model="m",
        provider="p",
        temperature=0.0,
        max_steps=1,
        created_at=now,
        updated_at=now,
    )
    assert a.created_at == now
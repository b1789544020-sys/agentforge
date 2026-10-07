"""Tests for the ``EvalRunner`` - both the legacy ``run`` and the new
``run_eval`` entry point.

These tests run against an in-memory SQLite DB (configured in
``conftest.py``) and use the ``mock`` LLM provider.
"""
from __future__ import annotations

import json
import os
import time
import uuid

import pytest

os.environ.setdefault("AGENTFORGE_LLM_PROVIDER", "mock")

# 0.1.2 5.1 轮: import ``app.database`` (the *module*) and reach
# into ``app.database.async_session_factory`` at call time.  This
# way the conftest's ``test_engine_factory`` fixture can monkey-patch
# the attribute and our tests transparently use the test in-memory
# engine - required because the eval runner fans cases out onto
# fresh sessions drawn from the same factory.
import app.database as _db_mod  # noqa: E402
from app.eval.runner import EvalRunner, RunEvalResult  # noqa: E402
from app.eval.scorer import StatusScorer, get_scorer  # noqa: E402
from app.models import Agent, EvalCase  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _create_agent(name: str = "ev-runner") -> Agent:
    async with _db_mod.async_session_factory() as s:
        a = Agent(
            name=name,
            system_prompt="You are a helpful assistant.",
            model="mock-model",
            provider="mock",
            temperature=0.0,
            max_steps=5,
            tools=[],
            config={},
        )
        s.add(a)
        await s.commit()
        await s.refresh(a)
        return a


async def _create_eval_case(
    *,
    input_: str = "hi",
    expected=None,
    meta=None,
    name: str = "case",
) -> EvalCase:
    async with _db_mod.async_session_factory() as s:
        row = EvalCase(
            id=str(uuid.uuid4()),
            eval_run_id=None,
            dataset="t",
            name=name,
            input=input_,
            expected=expected,
            meta=json.dumps(meta or {}),
            index=0,
            output="",
            score=0.0,
            passed=0,
            duration_ms=0,
            tokens=0,
            error=None,
            tool_calls="[]",
            score_details="{}",
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def _create_eval_cases(n: int, dataset: str = "concurrent") -> list[EvalCase]:
    rows: list[EvalCase] = []
    async with _db_mod.async_session_factory() as s:
        for i in range(n):
            row = EvalCase(
                id=str(uuid.uuid4()),
                eval_run_id=None,
                dataset=dataset,
                name=f"case-{i}",
                input=f"hi-{i}",
                expected="Mock response (no upstream call).",
                meta=json.dumps({}),
                index=i,
                output="",
                score=0.0,
                passed=0,
                duration_ms=0,
                tokens=0,
                error=None,
                tool_calls="[]",
                score_details="{}",
            )
            s.add(row)
            rows.append(row)
        await s.commit()
        for r in rows:
            await s.refresh(r)
    return rows


# ---------------------------------------------------------------------------
# run_eval - happy path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_eval_creates_run_and_per_case_rows() -> None:
    agent = await _create_agent()
    c1 = await _create_eval_case(input_="hello", expected="hi", name="c1")
    c2 = await _create_eval_case(input_="goodbye", expected="bye", name="c2")

    progress_calls = []

    def on_progress(done, total):
        progress_calls.append((done, total))

    async with _db_mod.async_session_factory() as s:
        ag = await s.get(Agent, agent.id)
        runner = EvalRunner(s, ag, pass_threshold=0.5, max_concurrency=1)
        result = await runner.run_eval(
            [c1.id, c2.id],
            scorer=get_scorer("status"),
            on_progress=on_progress,
        )

    assert isinstance(result, RunEvalResult)
    assert result.total == 2
    assert result.passed == 2
    # The progress callback was invoked (start + per-case).
    assert progress_calls, "on_progress was never called"
    assert progress_calls[-1] == (2, 2)


@pytest.mark.asyncio
async def test_run_eval_uses_composite_scorer_by_default() -> None:
    agent = await _create_agent()
    c = await _create_eval_case(input_="hi", expected="hi")
    async with _db_mod.async_session_factory() as s:
        ag = await s.get(Agent, agent.id)
        runner = EvalRunner(s, ag, max_concurrency=1)
        result = await runner.run_eval([c.id])
    assert result.scorer_name == "composite"


@pytest.mark.asyncio
async def test_run_eval_empty_id_list() -> None:
    agent = await _create_agent()
    async with _db_mod.async_session_factory() as s:
        ag = await s.get(Agent, agent.id)
        runner = EvalRunner(s, ag, max_concurrency=1)
        result = await runner.run_eval([])
    assert result.total == 0
    assert result.cases == []


@pytest.mark.asyncio
async def test_run_eval_preserves_requested_order() -> None:
    agent = await _create_agent()
    rows = []
    for i in range(5):
        rows.append(
            await _create_eval_case(input_=f"in-{i}", expected=f"out-{i}", name=f"o{i}")
        )
    shuffled = [rows[2], rows[0], rows[4], rows[1], rows[3]]
    async with _db_mod.async_session_factory() as s:
        ag = await s.get(Agent, agent.id)
        runner = EvalRunner(s, ag, max_concurrency=1)
        result = await runner.run_eval([r.id for r in shuffled])
    # The result cases should be in the order we requested.
    assert [c.input for c in result.cases] == [r.input for r in shuffled]


@pytest.mark.asyncio
async def test_run_eval_uses_expected_tool_from_meta() -> None:
    agent = await _create_agent()
    c = await _create_eval_case(
        input_="anything", expected="anything", meta={"expected_tool": "echo"}
    )
    async with _db_mod.async_session_factory() as s:
        ag = await s.get(Agent, agent.id)
        runner = EvalRunner(s, ag, max_concurrency=1)
        result = await runner.run_eval([c.id], scorer=StatusScorer())

    # Status scorer ignores the expected_tool, but the runner should
    # have surfaced it on the case so the UI can display it.
    assert result.cases[0].expected_tool == "echo"


# ---------------------------------------------------------------------------
# Legacy run() still works
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_legacy_run_still_works() -> None:
    # The legacy ``run()`` API should still work and return a populated
    # ``EvalResult``.  The mock LLM does not echo - it returns a fixed
    # ``"Mock response (no upstream call)."`` for every turn - so we
    # craft test cases whose ``expected`` field matches the canned
    # response, and use a custom scorer that grades on exact equality.
    agent = await _create_agent("legacy")
    MOCK_REPLY = "Mock response (no upstream call)."
    async with _db_mod.async_session_factory() as s:
        ag = await s.get(Agent, agent.id)
        runner = EvalRunner(
            s, ag, scorer=lambda pred, exp: 1.0 if pred == exp else 0.0
        )
        result = await runner.run(
            [
                {"input": "anything-1", "expected": MOCK_REPLY},
                {"input": "anything-2", "expected": MOCK_REPLY},
            ],
            dataset_name="legacy",
        )
    assert result.total == 2
    assert result.passed == 2
    assert result.failed == 0


# ---------------------------------------------------------------------------
# 0.1.2 5.1 轮: true concurrency tests (asyncio.gather + semaphore)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrency_runs_cases_in_parallel() -> None:
    """When ``max_concurrency>1`` the cases finish faster than the
    serial baseline.  We measure wall time on a 4-case suite: serial
    mock LLM adds ~5-10ms per case, so concurrent execution should
    noticeably cut the total.  The bound is loose to absorb
    scheduler jitter on CI."""
    agent = await _create_agent("concurrent-1")
    cases = await _create_eval_cases(4)
    slow_scorer = get_scorer("status")

    # Baseline: max_concurrency=1 (serial).
    async with _db_mod.async_session_factory() as s:
        ag = await s.get(Agent, agent.id)
        runner = EvalRunner(s, ag, scorer=slow_scorer, max_concurrency=1)
        t0 = time.perf_counter()
        result_serial = await runner.run_eval([c.id for c in cases])
        serial_ms = (time.perf_counter() - t0) * 1000

    # Concurrent: max_concurrency=4.
    async with _db_mod.async_session_factory() as s:
        ag = await s.get(Agent, agent.id)
        runner = EvalRunner(s, ag, scorer=slow_scorer, max_concurrency=4)
        t0 = time.perf_counter()
        result_conc = await runner.run_eval([c.id for c in cases])
        conc_ms = (time.perf_counter() - t0) * 1000

    assert result_serial.total == 4
    assert result_conc.total == 4
    # The concurrent run should be faster than the serial run.
    # Allow a wide margin (the LLM is a mock with no real I/O) but
    # assert at least *some* speedup so a regression that reverts
    # to serial will fail loudly.
    assert conc_ms < serial_ms, (
        f"concurrent run ({conc_ms:.0f}ms) was not faster than "
        f"serial ({serial_ms:.0f}ms)"
    )


@pytest.mark.asyncio
async def test_concurrency_results_match_requested_order() -> None:
    """``EvalRunner.run_eval`` returns cases in the order of the
    requested ``test_case_ids`` even when they completed in a
    different order (asyncio.gather resolves in completion order).
    We verify by matching the ``input`` field of each result back
    to the original test case rows, reversing the request order
    so a regression that reverts to completion order would fail.
    """
    agent = await _create_agent("concurrent-2")
    cases = await _create_eval_cases(6)
    # Reverse the request order to catch any accidental
    # completion-order sort.
    requested_rows = list(reversed(cases))
    requested_ids = [c.id for c in requested_rows]
    async with _db_mod.async_session_factory() as s:
        ag = await s.get(Agent, agent.id)
        runner = EvalRunner(
            s, ag, scorer=get_scorer("status"), max_concurrency=4
        )
        result = await runner.run_eval(requested_ids)
    # The case inputs should mirror the reversed order we requested.
    assert [c.input for c in result.cases] == [r.input for r in requested_rows]


@pytest.mark.asyncio
async def test_concurrency_legacy_run_returns_all_cases() -> None:
    """The legacy ``run`` path also got concurrent execution in
    0.1.2 5.1 轮; verify all cases complete and the per-case
    row is hydrated."""
    agent = await _create_agent("concurrent-3")
    async with _db_mod.async_session_factory() as s:
        ag = await s.get(Agent, agent.id)
        runner = EvalRunner(
            s,
            ag,
            scorer=lambda pred, exp: 1.0 if pred == exp else 0.0,
            max_concurrency=3,
        )
        result = await runner.run(
            [
                {"input": f"hi-{i}", "expected": "Mock response (no upstream call)."}
                for i in range(3)
            ],
            dataset_name="concurrent-legacy",
        )
    assert result.total == 3
    assert result.passed == 3
    # ``CaseResult`` does not carry the dataset name; verify by input
    # so we don't depend on the case row layout.
    assert {r.input for r in result.cases} == {"hi-0", "hi-1", "hi-2"}


@pytest.mark.asyncio
async def test_concurrency_max_concurrency_one_preserves_serial_behavior() -> None:
    """``max_concurrency=1`` falls back to serial execution.  This
    preserves 0.1.0 behaviour for callers that pass it explicitly."""
    agent = await _create_agent("concurrent-4")
    cases = await _create_eval_cases(2)
    async with _db_mod.async_session_factory() as s:
        ag = await s.get(Agent, agent.id)
        runner = EvalRunner(
            s, ag, scorer=get_scorer("status"), max_concurrency=1
        )
        result = await runner.run_eval([c.id for c in cases])
    assert result.total == 2
    # Order must be preserved (serial => already in request order).
    assert [c.index for c in result.cases] == [0, 1]


# 0.1.2 5.1 轮: every test in this module installs a
# ``test_engine_factory`` to make the eval runner's
# ``AsyncSessionLocal`` fallback resolve to a sessionmaker bound
# to the test's shared-cache in-memory engine (not the production
# file-backed engine).  This is needed because the runner fans
# out cases onto fresh sessions and they must see the same data
# as the test's own ``async_session_factory`` calls.
pytestmark = pytest.mark.usefixtures("test_engine_factory")

"""Tests for the ``app.eval.scorer`` module.

Covers the per-scorer logic, the composite scorer, the
``get_scorer`` factory and the ``text_similarity`` helper.
"""
from __future__ import annotations

import pytest

from app.eval.scorer import (
    AgentRunResult,
    AnswerScorer,
    CompositeScorer,
    EvalScore,
    StatusScorer,
    TestCase,
    ToolScorer,
    default_scorer,
    get_scorer,
    text_similarity,
)


def _ok_result(output: str = "Paris", status: str = "completed") -> AgentRunResult:
    return AgentRunResult(
        run_id="r1",
        output=output,
        status=status,
        steps=1,
        total_tokens=10,
        tool_calls=[],
    )


# ---------------------------------------------------------------------------
# text_similarity
# ---------------------------------------------------------------------------
def test_text_similarity_identical() -> None:
    assert text_similarity("hello world", "hello world") == 1.0


def test_text_similarity_empty_expected_is_vacuous() -> None:
    assert text_similarity("anything", "") == 1.0


def test_text_similarity_no_overlap() -> None:
    assert text_similarity("alpha beta", "gamma delta") == 0.0


def test_text_similarity_partial() -> None:
    sim = text_similarity("the quick brown fox", "the quick red fox")
    assert 0.0 < sim < 1.0


def test_text_similarity_case_and_whitespace() -> None:
    # Whitespace + case differences should not drop the score to 0.
    assert text_similarity("Paris", "  paris  ") == 1.0


# ---------------------------------------------------------------------------
# StatusScorer
# ---------------------------------------------------------------------------
def test_status_scorer_passes_on_completed() -> None:
    s = StatusScorer()
    out = s.score(_ok_result(status="completed"), TestCase(id="t", input="x"))
    assert out.score == 1.0
    assert out.passed is True


def test_status_scorer_passes_on_success_and_ok() -> None:
    s = StatusScorer()
    for status in ("success", "ok"):
        out = s.score(_ok_result(status=status), TestCase(id="t", input="x"))
        assert out.score == 1.0
        assert out.passed is True


def test_status_scorer_fails_on_failed() -> None:
    s = StatusScorer()
    out = s.score(_ok_result(status="failed"), TestCase(id="t", input="x"))
    assert out.score == 0.0
    assert out.passed is False
    assert out.details["status"] == "failed"


def test_status_scorer_zero_when_error_set_even_if_status_completed() -> None:
    s = StatusScorer()
    r = _ok_result(status="completed")
    r.error = "boom"
    out = s.score(r, TestCase(id="t", input="x"))
    assert out.score == 0.0


# ---------------------------------------------------------------------------
# ToolScorer
# ---------------------------------------------------------------------------
def test_tool_scorer_no_expected_is_skipped() -> None:
    s = ToolScorer()
    out = s.score(_ok_result(), TestCase(id="t", input="x"))
    assert out.score == 1.0
    assert out.details.get("skipped") is True


def test_tool_scorer_hits_expected() -> None:
    s = ToolScorer()
    r = _ok_result()
    r.tool_calls = [{"name": "search"}, {"name": "calc"}]
    out = s.score(r, TestCase(id="t", input="x", expected_tool="calc"))
    assert out.score == 1.0
    assert out.details["hit"] is True


def test_tool_scorer_misses() -> None:
    s = ToolScorer()
    r = _ok_result()
    r.tool_calls = [{"name": "search"}]
    out = s.score(r, TestCase(id="t", input="x", expected_tool="calc"))
    assert out.score == 0.0
    assert out.passed is False


# ---------------------------------------------------------------------------
# AnswerScorer
# ---------------------------------------------------------------------------
def test_answer_scorer_no_expected_is_skipped() -> None:
    s = AnswerScorer()
    out = s.score(_ok_result("hi"), TestCase(id="t", input="x"))
    assert out.score == 1.0
    assert out.details.get("skipped") is True


def test_answer_scorer_exact_match() -> None:
    s = AnswerScorer()
    out = s.score(
        _ok_result("Paris"),
        TestCase(id="t", input="x", expected_answer="Paris"),
    )
    assert out.score == 1.0
    assert out.passed is True


def test_answer_scorer_partial_match() -> None:
    s = AnswerScorer()
    out = s.score(
        _ok_result("The capital of France is Paris."),
        TestCase(
            id="t",
            input="x",
            expected_answer="Paris is the capital of France",
        ),
    )
    assert 0.0 < out.score < 1.0


# ---------------------------------------------------------------------------
# CompositeScorer
# ---------------------------------------------------------------------------
def test_composite_default_includes_status_tool_answer() -> None:
    s = default_scorer()
    names = [x.name for x in s.scorers]
    assert names == ["status", "tool", "answer"]


def test_composite_all_pass() -> None:
    s = CompositeScorer()
    r = _ok_result("Paris")
    r.tool_calls = [{"name": "lookup"}]
    out = s.score(
        r,
        TestCase(id="t", input="x", expected_answer="Paris", expected_tool="lookup"),
    )
    assert out.score == 1.0
    assert out.passed is True
    assert {c["name"] for c in out.details["components"]} == {"status", "tool", "answer"}


def test_composite_partial_failure_lowers_score() -> None:
    s = CompositeScorer()
    r = _ok_result("Paris")
    r.tool_calls = []  # missing expected_tool
    out = s.score(
        r,
        TestCase(id="t", input="x", expected_answer="Paris", expected_tool="lookup"),
    )
    # status=1.0, tool=0.0, answer=1.0 鈫?mean 0.666..
    assert 0.6 < out.score < 0.8
    assert out.passed is False  # tool sub-scorer failed


def test_composite_skips_components_with_no_expectation() -> None:
    """When ``expected_answer`` and ``expected_tool`` are missing, only
    ``status`` should contribute and the composite should pass."""
    s = CompositeScorer()
    r = _ok_result("anything")
    out = s.score(r, TestCase(id="t", input="x"))
    # status=1.0, tool skipped, answer skipped 鈫?mean of [1.0] = 1.0
    assert out.score == 1.0
    assert out.passed is True
    components = out.details["components"]
    skipped = [c for c in components if c["details"].get("skipped")]
    assert len(skipped) == 2


# ---------------------------------------------------------------------------
# get_scorer factory
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,cls",
    [
        ("default", CompositeScorer),
        ("composite", CompositeScorer),
        ("status", StatusScorer),
        ("tool", ToolScorer),
        ("answer", AnswerScorer),
    ],
)
def test_get_scorer_known(name: str, cls: type) -> None:
    s = get_scorer(name)
    assert isinstance(s, cls)


def test_get_scorer_unknown_falls_back_to_default() -> None:
    s = get_scorer("does-not-exist")
    assert isinstance(s, CompositeScorer)


def test_get_scorer_none_returns_default() -> None:
    s = get_scorer(None)
    assert isinstance(s, CompositeScorer)


# ---------------------------------------------------------------------------
# EvalScore.from_score
# ---------------------------------------------------------------------------
def test_eval_score_from_score_default_pass_at_07() -> None:
    e = EvalScore.from_score(0.7)
    assert e.passed is True
    e = EvalScore.from_score(0.69)
    assert e.passed is False


def test_eval_score_from_score_explicit_pass() -> None:
    e = EvalScore.from_score(0.1, passed=True, note="manual")
    assert e.score == 0.1
    assert e.passed is True
    assert e.details["note"] == "manual"

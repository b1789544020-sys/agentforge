"""Scoring primitives for the evaluation system.

This module defines:

* :class:`TestCase`         — a single test case (input + expectations)
* :class:`AgentRunResult`   — the result of one agent run inside an eval
* :class:`EvalScore`        — the per-case score produced by a scorer
* :class:`EvalScorer`       — base class / interface for scorers
* :class:`CompositeScorer`  — combines multiple scorers
* :class:`StatusScorer`     — checks ``status == completed``
* :class:`ToolScorer`       — checks tool calls (when ``expected_tool`` given)
* :class:`AnswerScorer`     — text similarity over ``expected_answer``

The contract requested by the design spec is::

    class EvalScorer:
        def score(self, result: AgentRunResult, test_case: TestCase) -> EvalScore: ...

Each subclass implements that single method.  All scorers return an
:class:`EvalScore` whose ``score`` field is a float in ``[0.0, 1.0]``.

The default factory :func:`default_scorer` returns a
:class:`CompositeScorer` that combines status, tool, and answer scorers.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass
class TestCase:
    """A single test case for evaluation.

    Attributes
    ----------
    id:
        Stable identifier (so results can be linked back to the case).
    input:
        The user-style task / question to feed the agent.
    expected_answer:
        Optional reference answer; compared with :class:`AnswerScorer`.
    expected_tool:
        Optional tool name that *must* appear in the trace.
    metadata:
        Free-form extra fields (e.g. ``{"difficulty": "easy"}``).
    """

    id: str
    input: str
    expected_answer: str | None = None
    expected_tool: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRunResult:
    """The result of running an agent against a single test case.

    This is a *normalized* view of an :class:`app.agent.loop.LoopResult`
    so the scorers don't have to depend on the loop's internals.
    """

    run_id: str
    output: str
    status: str  # e.g. "completed", "failed", "timeout", "terminated"
    steps: int = 0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class EvalScore:
    """Score produced by an :class:`EvalScorer`."""

    score: float
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_score(
        cls, score: float, *, passed: bool | None = None, **details: Any
    ) -> "EvalScore":
        return cls(
            score=float(score),
            passed=bool(score >= 0.7) if passed is None else bool(passed),
            details=dict(details),
        )


# ---------------------------------------------------------------------------
# Scorer base / interface
# ---------------------------------------------------------------------------


class EvalScorer:
    """Base class for all evaluation scorers.

    Subclasses must override :meth:`score`.  The method receives an
    :class:`AgentRunResult` and the :class:`TestCase` it was produced
    for, and returns an :class:`EvalScore`.
    """

    name: str = "base"

    def score(
        self, result: AgentRunResult, test_case: TestCase
    ) -> EvalScore:  # pragma: no cover - interface
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Individual scorers
# ---------------------------------------------------------------------------


# status values that count as "task completed" (case-insensitive match)
_COMPLETED_STATUSES = {"completed", "success", "ok"}


class StatusScorer(EvalScorer):
    """Reward runs that ended with a "completed" status.

    A run is considered completed if ``result.status`` is in
    ``{"completed", "success", "ok"}``.  Anything else (failed, timeout,
    terminated, error) returns 0.0.
    """

    name = "status"

    def score(
        self, result: AgentRunResult, test_case: TestCase
    ) -> EvalScore:
        completed = str(result.status or "").lower() in _COMPLETED_STATUSES
        return EvalScore.from_score(
            1.0 if completed and not result.error else 0.0,
            completed=completed,
            status=result.status,
        )


class ToolScorer(EvalScorer):
    """Check whether the expected tool was called.

    Only contributes when ``test_case.expected_tool`` is set.  Otherwise
    returns a neutral ``1.0`` (no expectation = no penalty).
    """

    name = "tool"

    def score(
        self, result: AgentRunResult, test_case: TestCase
    ) -> EvalScore:
        expected = (test_case.expected_tool or "").strip()
        if not expected:
            return EvalScore.from_score(1.0, skipped=True)
        called_names = [
            (tc.get("name") or tc.get("tool") or "").strip()
            for tc in result.tool_calls
        ]
        hit = expected in called_names
        return EvalScore.from_score(
            1.0 if hit else 0.0,
            expected_tool=expected,
            called_tools=called_names,
            hit=hit,
        )


_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(_normalize(text))


def _token_f1(prediction: str, expected: str) -> float:
    p = set(_tokenize(prediction))
    r = set(_tokenize(expected))
    if not p and not r:
        return 1.0
    if not p or not r:
        return 0.0
    overlap = p & r
    if not overlap:
        return 0.0
    precision = len(overlap) / len(p)
    recall = len(overlap) / len(r)
    return 2 * precision * recall / (precision + recall)


def _rouge_l(prediction: str, expected: str) -> float:
    a = _tokenize(expected)
    b = _tokenize(prediction)
    if not a or not b:
        return 0.0
    matcher = SequenceMatcher(a=a, b=b, autojunk=False)
    lcs = matcher.find_longest_match(
        0, len(a), 0, len(b)
    ).size
    if lcs == 0:
        return 0.0
    precision = lcs / len(b)
    recall = lcs / len(a)
    beta = 1.0
    denom = precision + recall
    if denom == 0:
        return 0.0
    return ((1 + beta * beta) * precision * recall) / (
        recall + beta * beta * precision
    )


def text_similarity(prediction: str, expected: str) -> float:
    """Return a similarity in [0, 1] between two strings.

    Uses token-F1 (primary) blended with ROUGE-L.  The blend is
    ``0.5 * token_f1 + 0.5 * rouge_l`` so a verbatim answer scores 1.0
    while partial overlap still produces something > 0.
    """
    if not expected:
        return 1.0
    return max(0.0, min(1.0, 0.5 * _token_f1(prediction, expected) + 0.5 * _rouge_l(prediction, expected)))


class AnswerScorer(EvalScorer):
    """Text-similarity scorer for ``expected_answer``.

    Only contributes when ``test_case.expected_answer`` is set.  Uses
    :func:`text_similarity` (token-F1 + ROUGE-L).
    """

    name = "answer"

    def score(
        self, result: AgentRunResult, test_case: TestCase
    ) -> EvalScore:
        expected = test_case.expected_answer
        if not expected:
            return EvalScore.from_score(1.0, skipped=True)
        sim = text_similarity(result.output or "", expected)
        return EvalScore.from_score(sim, similarity=sim, expected=expected)


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


class CompositeScorer(EvalScorer):
    """Combines multiple scorers into one.

    The composite's final score is the **mean** of its sub-scorers'
    scores (sub-scorers marked ``skipped=True`` are excluded from the
    average).  The composite is considered passed only if all non-skipped
    sub-scorers are passed.
    """

    name = "composite"

    def __init__(self, scorers: Iterable[EvalScorer] | None = None) -> None:
        if scorers is None:
            scorers = [StatusScorer(), ToolScorer(), AnswerScorer()]
        self.scorers: list[EvalScorer] = list(scorers)

    def score(
        self, result: AgentRunResult, test_case: TestCase
    ) -> EvalScore:
        sub: list[EvalScore] = []
        for s in self.scorers:
            sub.append(s.score(result, test_case))
        # Filter out skipped components
        active = [
            x
            for x in sub
            if not x.details.get("skipped", False)
        ] or sub
        score = sum(x.score for x in active) / len(active) if active else 0.0
        all_passed = all(x.passed for x in active)
        return EvalScore(
            score=float(score),
            passed=bool(all_passed),
            details={
                "components": [
                    {
                        "name": s.name,
                        "score": x.score,
                        "passed": x.passed,
                        "details": x.details,
                    }
                    for s, x in zip(self.scorers, sub)
                ]
            },
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def default_scorer() -> CompositeScorer:
    """Return the default composite scorer (status + tool + answer)."""
    return CompositeScorer()


def get_scorer(name: str | None) -> EvalScorer:
    """Resolve a scorer by name (for API requests).

    Supported names: ``"default"``, ``"composite"``, ``"status"``,
    ``"tool"``, ``"answer"``.  ``None`` and unknown names return the
    default composite scorer.
    """
    if not name or name in ("default", "composite"):
        return default_scorer()
    if name == "status":
        return StatusScorer()
    if name == "tool":
        return ToolScorer()
    if name == "answer":
        return AnswerScorer()
    return default_scorer()


__all__ = [
    "TestCase",
    "AgentRunResult",
    "EvalScore",
    "EvalScorer",
    "StatusScorer",
    "ToolScorer",
    "AnswerScorer",
    "CompositeScorer",
    "default_scorer",
    "get_scorer",
    "text_similarity",
]
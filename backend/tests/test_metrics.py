"""Tests for the built-in eval scorers."""
from __future__ import annotations

from app.eval.metrics import Metrics


def test_exact_match_passes() -> None:
    s = Metrics.get("exact_match")
    assert s("Paris", "Paris") == 1.0
    assert s("Paris", "paris") == 0.0
    assert s("", "") == 1.0


def test_contains_match() -> None:
    s = Metrics.get("contains")
    assert s("The quick brown fox", "quick") == 1.0
    assert s("The quick brown fox", "lazy") == 0.0
    # Case-insensitive by default
    assert s("Quick", "quick") == 1.0


def test_registry_contains_defaults() -> None:
    names = set(Metrics.registry())
    assert {"exact_match", "contains", "json_match"}.issubset(names)


def test_unknown_scorer_raises() -> None:
    try:
        Metrics.get("does-not-exist")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown scorer")
"""Scoring / metric helpers used by :class:`EvalRunner`."""
from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Any


_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(_normalize(text))


def _to_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return set(_tokenize(value))
    if isinstance(value, Iterable):
        out: set[str] = set()
        for item in value:
            out.update(_to_set(item))
        return out
    return {str(value)}


class Metrics:
    """Collection of callable scorers, all returning floats in [0, 1]."""

    @staticmethod
    def exact_match(prediction: str, expected: Any) -> float:
        if expected is None:
            return 1.0
        return 1.0 if str(prediction) == str(expected) else 0.0

    @staticmethod
    def contains_match(prediction: str, expected: Any) -> float:
        if expected is None:
            return 1.0
        p = _normalize(str(prediction))
        if isinstance(expected, Iterable) and not isinstance(expected, str):
            targets = [_normalize(str(e)) for e in expected if e is not None]
        else:
            targets = [_normalize(str(expected))]
        if not targets:
            return 1.0
        hits = sum(1 for t in targets if t and t in p)
        return hits / len(targets)

    @staticmethod
    def rouge_l(prediction: str, expected: Any) -> float:
        if expected is None:
            return 1.0
        pred_tokens = _tokenize(str(prediction))
        ref_tokens = _tokenize(str(expected))
        if not pred_tokens or not ref_tokens:
            return 0.0
        matcher = SequenceMatcher(a=ref_tokens, b=pred_tokens, autojunk=False)
        lcs = matcher.find_longest_match(0, len(ref_tokens), 0, len(pred_tokens)).size
        if lcs == 0:
            return 0.0
        precision = lcs / len(pred_tokens)
        recall = lcs / len(ref_tokens)
        beta = 1.0
        denom = precision + recall
        if denom == 0:
            return 0.0
        return ((1 + beta * beta) * precision * recall) / (recall + beta * beta * precision)

    @staticmethod
    def token_f1(prediction: str, expected: Any) -> float:
        if expected is None:
            return 1.0
        p = _to_set(prediction)
        r = _to_set(expected)
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

    @staticmethod
    def numeric_close(prediction: str, expected: Any, *, atol: float = 1e-3) -> float:
        """Reward numeric answers within an absolute tolerance."""
        if expected is None:
            return 1.0
        m = re.search(r"-?\d+(?:\.\d+)?", str(prediction))
        if not m:
            return 0.0
        try:
            value = float(m.group(0))
        except ValueError:
            return 0.0
        try:
            target = float(expected)
        except (TypeError, ValueError):
            return 0.0
        return 1.0 if abs(value - target) <= atol else max(0.0, 1.0 - abs(value - target) / max(abs(target), 1.0))

    @staticmethod
    def json_match(prediction: str, expected: Any) -> float:
        """Parse JSON from prediction and compare to expected (subset check)."""
        import json as _json

        if expected is None:
            return 1.0
        m = re.search(r"\{.*\}|\[.*\]", str(prediction), re.DOTALL)
        if not m:
            return 0.0
        try:
            data = _json.loads(m.group(0))
        except ValueError:
            return 0.0
        if isinstance(expected, str):
            try:
                expected = _json.loads(expected)
            except ValueError:
                expected = {"text": expected}
        if not isinstance(expected, dict):
            return 1.0 if data == expected else 0.0
        if not isinstance(data, dict):
            return 0.0
        matches = 0
        for k, v in expected.items():
            if k in data and data[k] == v:
                matches += 1
        return matches / max(1, len(expected))

    @classmethod
    def registry(cls) -> dict[str, Any]:
        return {
            "exact_match": cls.exact_match,
            "contains": cls.contains_match,
            "rouge_l": cls.rouge_l,
            "token_f1": cls.token_f1,
            "numeric": cls.numeric_close,
            "json_match": cls.json_match,
        }

    @classmethod
    def get(cls, name: str) -> Any:
        """Look up a scorer by name; raise :class:`KeyError` if unknown."""
        scorer = cls.registry().get(name)
        if scorer is None:
            raise KeyError(f"unknown scorer: {name!r}")
        return scorer


__all__ = ["Metrics"]
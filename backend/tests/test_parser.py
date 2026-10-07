"""Unit tests for ``app.eval.parser`` (0.1.3 #2).

These tests are pure — no DB, no FastAPI — and exercise the JSONL
and CSV parsers, the format dispatcher and the
:class:`DatasetParseError` round-trip.
"""
from __future__ import annotations

import pytest

from app.eval.parser import (
    DatasetParseError,
    ParseError,
    ParsedCase,
    detect_format,
    parse_csv,
    parse_dataset,
    parse_jsonl,
)


# ---------------------------------------------------------------------------
# parse_jsonl
# ---------------------------------------------------------------------------
def test_parse_jsonl_happy_path() -> None:
    text = (
        '{"input": "What is 2+2?", "expected": "4", "name": "arithmetic", "tags": ["math", "easy"]}\n'
        '{"input": "Capital of France?", "expected": "Paris"}\n'
        '\n'  # blank line — must be skipped
        '{"input": "What colour is the sky?", "expected": "blue"}\n'
    )
    cases = parse_jsonl(text)
    assert len(cases) == 3
    assert cases[0].input == "What is 2+2?"
    assert cases[0].expected == "4"
    assert cases[0].name == "arithmetic"
    assert cases[0].tags == ["math", "easy"]
    assert cases[1].expected == "Paris"
    assert cases[1].name is None
    assert cases[1].tags == []
    assert cases[2].expected == "blue"


def test_parse_jsonl_extra_fields_become_metadata() -> None:
    cases = parse_jsonl(
        '{"input": "q", "expected": "a", "name": "n", '
        '"tags": ["t"], "source": "seed1", "weight": 2}\n'
    )
    assert cases[0].metadata == {"source": "seed1", "weight": 2}


def test_parse_jsonl_missing_input_collects_error() -> None:
    text = (
        '{"expected": "no input"}\n'  # line 1: missing 'input'
        '{"input": 42}\n'  # line 2: input is not a string
        '{not json\n'  # line 3: bad JSON
        '{"input": "ok"}\n'  # line 4: valid
    )
    with pytest.raises(DatasetParseError) as ei:
        parse_jsonl(text)
    errs = ei.value.errors
    assert len(errs) == 3
    assert errs[0].line == 1
    assert "input" in errs[0].message
    assert errs[1].line == 2
    assert "must be a string" in errs[1].message
    assert errs[2].line == 3
    assert "invalid JSON" in errs[2].message


def test_parse_jsonl_rejects_top_level_array() -> None:
    with pytest.raises(ValueError, match="JSON arrays are not supported"):
        parse_dataset('[{"input": "x"}]')


def test_parse_jsonl_omitted_optional_fields() -> None:
    cases = parse_jsonl('{"input": "hello"}\n')
    assert cases[0].input == "hello"
    assert cases[0].expected is None
    assert cases[0].name is None
    assert cases[0].tags == []
    payload = cases[0].to_runner_payload()
    assert payload == {"input": "hello"}


# ---------------------------------------------------------------------------
# parse_csv
# ---------------------------------------------------------------------------
def test_parse_csv_happy_path() -> None:
    text = (
        "input,expected,name,tags,source\n"
        "What is 2+2?,4,arithmetic,math;easy,seed1\n"
        "Capital of France?,Paris,geo,geography,seed2\n"
    )
    cases = parse_csv(text)
    assert len(cases) == 2
    assert cases[0].input == "What is 2+2?"
    assert cases[0].expected == "4"
    assert cases[0].tags == ["math", "easy"]
    assert cases[0].metadata == {"source": "seed1"}
    assert cases[1].metadata == {"source": "seed2"}


def test_parse_csv_missing_required_column_raises() -> None:
    with pytest.raises(DatasetParseError) as ei:
        parse_csv("expected,name\n4,arithmetic\n")
    assert ei.value.errors[0].line == 1
    assert "input" in ei.value.errors[0].message


def test_parse_csv_empty_input_cell_collects_error() -> None:
    text = "input,expected\n,4\n"  # input cell is blank
    with pytest.raises(DatasetParseError) as ei:
        parse_csv(text)
    assert ei.value.errors[0].line == 2
    assert "empty" in ei.value.errors[0].message


def test_parse_csv_unknown_columns_fold_into_metadata() -> None:
    text = "input,custom1,custom2\nq,v1,v2\n"
    cases = parse_csv(text)
    assert cases[0].metadata == {"custom1": "v1", "custom2": "v2"}


# ---------------------------------------------------------------------------
# detect_format / parse_dataset dispatcher
# ---------------------------------------------------------------------------
def test_detect_format_by_filename() -> None:
    assert detect_format("ds.jsonl", "") == "jsonl"
    assert detect_format("ds.ndjson", "") == "jsonl"
    assert detect_format("ds.JSONL", "") == "jsonl"
    assert detect_format("ds.csv", "") == "csv"
    assert detect_format("ds.json", "") == "jsonl"


def test_detect_format_by_content() -> None:
    assert detect_format(None, '{}\n') == "jsonl"
    assert detect_format(None, "a,b\n1,2\n") == "csv"
    with pytest.raises(ValueError):
        detect_format(None, "")
    with pytest.raises(ValueError, match="could not detect"):
        detect_format(None, "no commas no braces\n")


def test_parse_dataset_dispatch() -> None:
    cases = parse_dataset('{"input": "q"}\n', filename="x.jsonl")
    assert len(cases) == 1
    cases = parse_dataset("input\nq\n", filename="x.csv")
    assert len(cases) == 1
    cases = parse_dataset('{"input": "q"}\n', fmt="jsonl")
    assert len(cases) == 1
    with pytest.raises(ValueError, match="unsupported"):
        parse_dataset("anything", fmt="xml")


# ---------------------------------------------------------------------------
# ParsedCase.to_runner_payload
# ---------------------------------------------------------------------------
def test_to_runner_payload_minimal() -> None:
    payload = ParsedCase(input="q").to_runner_payload()
    assert payload == {"input": "q"}


def test_to_runner_payload_full() -> None:
    case = ParsedCase(
        input="q",
        expected="a",
        name="n",
        tags=["t1", "t2"],
        metadata={"k": "v"},
    )
    assert case.to_runner_payload() == {
        "input": "q",
        "expected": "a",
        "metadata": {"name": "n", "tags": ["t1", "t2"], "k": "v"},
    }


def test_to_runner_payload_omits_empty_metadata() -> None:
    assert ParsedCase(input="q", expected=None, name=None).to_runner_payload() == {
        "input": "q",
    }


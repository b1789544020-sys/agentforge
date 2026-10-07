"""Dataset parsers for the eval upload endpoint (0.1.3 #2).

Two formats are supported out of the box:

* **JSONL** — one JSON object per line.  Every object must contain an
  ``input`` key (str).  ``expected`` (any), ``name`` (str) and
  ``tags`` (list[str]) are optional.
* **CSV** — header row plus one case per row.  Required column:
  ``input``.  Optional: ``expected`` (stringified on the way in),
  ``name`` (str), ``tags`` (semicolon-separated list).

Both functions are pure: they take raw text + an optional filename
hint and return a list of :class:`ParsedCase` dataclasses.  Invalid
documents raise :class:`DatasetParseError` which carries per-line
records for an HTTP 400 response.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any

_MAX_UPLOAD_BYTES_DEFAULT = 10 * 1024 * 1024


class DatasetParseError(ValueError):
    def __init__(self, errors: list["ParseError"]) -> None:
        self.errors = errors
        msg = "; ".join(f"line {e.line}: {e.message}" for e in errors[:5])
        if len(errors) > 5:
            msg += f" (and {len(errors) - 5} more)"
        super().__init__(msg)


@dataclass
class ParseError:
    line: int
    message: str
    column: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"line": self.line, "message": self.message}
        if self.column is not None:
            out["column"] = self.column
        return out


@dataclass
class ParsedCase:
    input: str
    expected: Any = None
    name: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_runner_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"input": self.input}
        if self.expected is not None:
            payload["expected"] = self.expected
        meta: dict[str, Any] = {}
        if self.name is not None:
            meta["name"] = self.name
        if self.tags:
            meta["tags"] = list(self.tags)
        meta.update(self.metadata)
        if meta:
            payload["metadata"] = meta
        return payload

# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
def detect_format(filename: str | None, text: str) -> str:
    if filename:
        lower = filename.lower()
        if lower.endswith((".jsonl", ".ndjson")):
            return "jsonl"
        if lower.endswith(".csv"):
            return "csv"
        if lower.endswith(".json"):
            return "jsonl"
    stripped = text.lstrip()
    if not stripped:
        raise ValueError("dataset is empty")
    if stripped.startswith("["):
        raise ValueError(
            "JSON arrays are not supported; please upload a .jsonl file "
            "with one case object per line"
        )
    if stripped.startswith("{"):
        return "jsonl"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("{"):
            return "jsonl"
        if "," in line:
            return "csv"
        break
    raise ValueError(
        "could not detect dataset format; pass a .jsonl or .csv file"
    )


def parse_dataset(
    text: str,
    filename: str | None = None,
    *,
    fmt: str | None = None,
    max_bytes: int = _MAX_UPLOAD_BYTES_DEFAULT,
) -> list[ParsedCase]:
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(
            f"dataset exceeds max size of {max_bytes} bytes "
            f"(got {len(text.encode('utf-8'))} bytes)"
        )
    chosen = (fmt or "").lower() or detect_format(filename, text)
    if chosen == "jsonl":
        return parse_jsonl(text)
    if chosen == "csv":
        return parse_csv(text)
    raise ValueError(f"unsupported dataset format: {chosen!r}")


# ---------------------------------------------------------------------------
# JSONL
# ---------------------------------------------------------------------------
def parse_jsonl(text: str) -> list[ParsedCase]:
    cases: list[ParsedCase] = []
    errors: list[ParseError] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(ParseError(line=lineno, message=f"invalid JSON: {e.msg}"))
            continue
        if not isinstance(obj, dict):
            errors.append(ParseError(
                line=lineno,
                message=f"expected JSON object, got {type(obj).__name__}",
            ))
            continue
        if "input" not in obj:
            errors.append(ParseError(
                line=lineno,
                message="missing required field 'input'",
            ))
            continue
        if not isinstance(obj["input"], str):
            errors.append(ParseError(
                line=lineno,
                message=(
                    "field 'input' must be a string, got "
                    f"{type(obj['input']).__name__}"
                ),
            ))
            continue
        cases.append(_build_case_from_object(obj))
    if errors:
        raise DatasetParseError(errors)
    return cases


def _build_case_from_object(obj: dict[str, Any]) -> ParsedCase:
    name = obj.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError(
            f"field 'name' must be a string, got {type(name).__name__}"
        )
    tags_raw = obj.get("tags")
    tags: list[str]
    if tags_raw is None:
        tags = []
    elif isinstance(tags_raw, str):
        tags = [t.strip() for t in tags_raw.split(";") if t.strip()]
    elif isinstance(tags_raw, list) and all(isinstance(t, str) for t in tags_raw):
        tags = list(tags_raw)
    else:
        raise ValueError(
            "field 'tags' must be a list of strings or a ';' separated string"
        )
    expected = obj.get("expected")
    reserved = {"input", "expected", "name", "tags"}
    metadata = {k: v for k, v in obj.items() if k not in reserved}
    return ParsedCase(
        input=obj["input"],
        expected=expected,
        name=name,
        tags=tags,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
_REQUIRED_CSV_COLUMNS = {"input"}
_KNOWN_CSV_COLUMNS = {"input", "expected", "name", "tags"}


def parse_csv(text: str) -> list[ParsedCase]:
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise DatasetParseError(
            [ParseError(line=1, message="CSV is empty (no header row)")]
        ) from None
    header = [h.strip() for h in header]
    if not header or all(h == "" for h in header):
        raise DatasetParseError(
            [ParseError(line=1, message="CSV header row is empty")]
        )
    missing = _REQUIRED_CSV_COLUMNS - set(header)
    if missing:
        raise DatasetParseError([
            ParseError(
                line=1,
                message=(
                    "CSV header missing required column(s): "
                    + ", ".join(sorted(missing))
                ),
            )
        ])
    header_lc = [h.lower() for h in header]
    col_index = {name: idx for idx, name in enumerate(header_lc)}
    cases: list[ParsedCase] = []
    errors: list[ParseError] = []
    for body_lineno, row in enumerate(reader, start=2):
        if not row or all((cell or "").strip() == "" for cell in row):
            continue
        if len(row) < len(header_lc):
            row = list(row) + [""] * (len(header_lc) - len(row))
        cell_input = (row[col_index["input"]] or "").strip()
        if not cell_input:
            errors.append(ParseError(
                line=body_lineno, message="column 'input' is empty"
            ))
            continue
        cell_expected: Any = None
        if "expected" in col_index:
            cell_expected = (row[col_index["expected"]] or "").strip()
            if cell_expected == "":
                cell_expected = None
        cell_name: str | None = None
        if "name" in col_index:
            cell_name = (row[col_index["name"]] or "").strip() or None
        tags: list[str] = []
        if "tags" in col_index:
            raw_tags = (row[col_index["tags"]] or "").strip()
            if raw_tags:
                tags = [t.strip() for t in raw_tags.split(";") if t.strip()]
        metadata: dict[str, Any] = {}
        for idx, col_name in enumerate(header_lc):
            if col_name in _KNOWN_CSV_COLUMNS:
                continue
            value = (row[idx] or "").strip()
            if value:
                metadata[col_name] = value
        cases.append(ParsedCase(
            input=cell_input,
            expected=cell_expected,
            name=cell_name,
            tags=tags,
            metadata=metadata,
        ))
    if errors:
        raise DatasetParseError(errors)
    return cases


__all__ = [
    "DatasetParseError",
    "ParseError",
    "ParsedCase",
    "detect_format",
    "parse_dataset",
    "parse_jsonl",
    "parse_csv",
]


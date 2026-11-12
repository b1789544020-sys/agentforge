"""Version-dispatching Readers that preserve raw legacy/unsupported records."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from app.quality.gate import GateRecordV1
from app.quality.run import RunRecordV1

RecordT = TypeVar("RecordT", RunRecordV1, GateRecordV1)
ReaderStatus = Literal["v1", "legacy_unavailable", "unsupported_version"]
_RUN_FIELDS = ("answer_output", "tool_output", "retrieval_output", "execution_output")
_GATE_FIELDS = ("conditions", "evidence")


@dataclass(frozen=True)
class QualityReadResult(Generic[RecordT]):
    status: ReaderStatus
    record: RecordT | None
    raw: dict[str, Any]


def _status(payload: Mapping[str, Any], observation_fields: tuple[str, ...]) -> ReaderStatus:
    version = payload.get("quality_record_version")
    if version is None:
        if any(payload.get(field) is not None for field in observation_fields):
            raise ValueError("legacy records cannot contain non-null quality observations")
        return "legacy_unavailable"
    if type(version) is not int:  # bool is deliberately not a record version.
        raise ValueError("quality_record_version must be an integer or null")
    if version != 1:
        return "unsupported_version"
    return "v1"


def read_run_record(payload: Mapping[str, Any]) -> QualityReadResult[RunRecordV1]:
    if not isinstance(payload, Mapping):
        raise TypeError("Run quality record must be a mapping")
    raw = copy.deepcopy(dict(payload))
    status = _status(payload, _RUN_FIELDS)
    record = RunRecordV1.model_validate(payload) if status == "v1" else None
    return QualityReadResult(status=status, record=record, raw=raw)


def read_gate_record(payload: Mapping[str, Any]) -> QualityReadResult[GateRecordV1]:
    if not isinstance(payload, Mapping):
        raise TypeError("Gate quality record must be a mapping")
    raw = copy.deepcopy(dict(payload))
    status = _status(payload, _GATE_FIELDS)
    record = GateRecordV1.model_validate(payload) if status == "v1" else None
    return QualityReadResult(status=status, record=record, raw=raw)


__all__ = [
    "QualityReadResult",
    "ReaderStatus",
    "read_gate_record",
    "read_run_record",
]

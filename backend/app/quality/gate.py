"""Gate v1 immutable conditions, per-attempt judgments, and incremental diffs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.agent.schema import assert_public_safe
from app.quality.common import (
    QUALITY_LAYERS,
    NonEmptyStr,
    QualityError,
    QualityLayer,
    QualityModel,
    Sha256,
    ensure_unique,
)
from app.quality.pointer import JsonPointerError, pointer_tokens, resolve_json_pointer
from app.quality.run import RunRecordV1

LayerStatus = Literal["passed", "failed", "not_evaluated", "not_available", "error"]
_LAYER_POINTERS: dict[QualityLayer, str] = {
    "output": "answer_output",
    "tool": "tool_output",
    "retrieval": "retrieval_output",
    "system": "execution_output",
}


class AssertionSubject(QualityModel):
    run_id: UUID
    pointer: str
    evidence_id: NonEmptyStr | None = None
    query_id: NonEmptyStr | None = None
    call_id: NonEmptyStr | None = None
    source_id: NonEmptyStr | None = None

    @field_validator("pointer")
    @classmethod
    def _observation_pointer(cls, value: str) -> str:
        tokens = pointer_tokens(value)
        if not tokens or tokens[0] not in _LAYER_POINTERS.values():
            raise ValueError("assertion pointer must identify a Run observation object")
        return value


class GateAssertion(QualityModel):
    assertion_id: NonEmptyStr
    kind: NonEmptyStr
    status: LayerStatus
    score: float | None = Field(default=None, ge=0, le=1)
    reason: str
    subject: AssertionSubject
    rule_version: NonEmptyStr | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class GateLayer(QualityModel):
    status: LayerStatus
    assertions: list[GateAssertion]

    @model_validator(mode="after")
    def _consistent_assertions(self) -> Self:
        ensure_unique([item.assertion_id for item in self.assertions], "assertion_id")
        if self.status == "passed" and any(
            item.status in {"failed", "not_available", "error"} for item in self.assertions
        ):
            raise ValueError("a passed layer cannot contain failed/error assertions")
        return self


class GateCaseLayers(QualityModel):
    output: GateLayer
    tool: GateLayer
    retrieval: GateLayer
    system: GateLayer

    def as_mapping(self) -> dict[QualityLayer, GateLayer]:
        return {layer: getattr(self, layer) for layer in QUALITY_LAYERS}


@dataclass(frozen=True)
class LayerAggregation:
    passed: bool
    failed_layers: tuple[QualityLayer, ...]
    not_evaluated_layers: tuple[QualityLayer, ...]


def aggregate_layers(
    layers: Mapping[str, GateLayer], required_layers: Iterable[str]
) -> LayerAggregation:
    required = tuple(dict.fromkeys(required_layers))
    unknown = set(required) - set(QUALITY_LAYERS)
    if unknown:
        raise ValueError(f"unknown required quality layer: {sorted(unknown)[0]}")
    failed: list[QualityLayer] = []
    not_evaluated: list[QualityLayer] = []
    blocking = False
    for layer in QUALITY_LAYERS:
        observed = layers.get(layer)
        status = observed.status if observed is not None else "not_available"
        if status == "not_evaluated":
            not_evaluated.append(layer)
        if status in {"failed", "error", "not_available"} or (
            layer in required and status != "passed"
        ):
            failed.append(layer)
        if layer in required and status != "passed":
            blocking = True
    return LayerAggregation(
        passed=not blocking,
        failed_layers=tuple(failed),
        not_evaluated_layers=tuple(not_evaluated),
    )


class GateCaseEvidence(QualityModel):
    evidence_id: NonEmptyStr
    case_id: NonEmptyStr
    result_case_id: NonEmptyStr | None
    attempt: int = Field(ge=1)
    run_id: UUID
    agent_version_id: UUID
    passed: bool
    failed_layers: list[QualityLayer]
    layers: GateCaseLayers
    error: QualityError | None

    @model_validator(mode="after")
    def _consistent_result(self) -> Self:
        ensure_unique(self.failed_layers, "failed layer")
        if not self.passed and not self.failed_layers and self.error is None:
            raise ValueError("a failed case requires failed layers or a structured error")
        return self


class GateCaseSnapshot(QualityModel):
    case_id: NonEmptyStr
    input: str
    expected: Any
    metadata: dict[str, Any]
    input_sha256: Sha256
    expected_sha256: Sha256
    metadata_sha256: Sha256


class GateScorer(QualityModel):
    name: NonEmptyStr
    version: NonEmptyStr
    config: dict[str, Any]
    config_sha256: Sha256


class GateSuiteConditions(QualityModel):
    suite_id: UUID
    case_ids: list[NonEmptyStr]
    case_snapshots: list[GateCaseSnapshot]
    scorer: GateScorer
    case_pass_threshold: float = Field(ge=0, le=1)
    gate_threshold_at_execution: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _stable_case_order(self) -> Self:
        ensure_unique(self.case_ids, "condition case_id")
        snapshot_ids = [item.case_id for item in self.case_snapshots]
        if snapshot_ids != self.case_ids:
            raise ValueError("case snapshots must exactly match the ordered condition case_ids")
        return self


class GateExecutionConditions(QualityModel):
    agent_version_id: UUID
    definition_sha256: Sha256
    provider: NonEmptyStr
    model: NonEmptyStr
    model_digest: Sha256 | None
    temperature: float = Field(ge=0)
    seed: int | None
    run_count: int = Field(ge=1)
    tool_definitions_sha256: Sha256


class GateRetrievalConditions(QualityModel):
    embedding_model: NonEmptyStr
    index_snapshot_id: NonEmptyStr
    corpus_manifest_sha256: Sha256
    top_k: int = Field(ge=1)
    embedding_provider: NonEmptyStr | None = None
    embedding_dimensions: int | None = Field(default=None, ge=1)


class GateComparisonConditions(QualityModel):
    allowed_changes: list[NonEmptyStr]
    required_invariants: list[NonEmptyStr]

    @model_validator(mode="after")
    def _unique_conditions(self) -> Self:
        ensure_unique(self.allowed_changes, "allowed change")
        ensure_unique(self.required_invariants, "required invariant")
        return self


class GateConditions(QualityModel):
    suite: GateSuiteConditions
    execution: GateExecutionConditions
    retrieval: GateRetrievalConditions | None
    comparison: GateComparisonConditions

    def required_layers(self) -> tuple[QualityLayer, ...]:
        configured = self.suite.scorer.config.get("required_layers", list(QUALITY_LAYERS))
        if not isinstance(configured, list) or any(
            not isinstance(item, str) for item in configured
        ):
            raise ValueError("scorer required_layers must be an array of layer names")
        unknown = set(configured) - set(QUALITY_LAYERS)
        if unknown:
            raise ValueError(f"unknown required quality layer: {sorted(unknown)[0]}")
        return tuple(configured)  # type: ignore[return-value]


class GateDetail(QualityModel):
    case_id: NonEmptyStr
    result_case_id: NonEmptyStr | None = None
    input: str
    expected: Any = None
    output: str | None
    score: float
    passed: bool
    error: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    run_ids: list[UUID] | None = None
    attempt_count: int | None = Field(default=None, ge=0)
    failed_layers: list[QualityLayer] | None = None


class GateLayerChange(QualityModel):
    layer: QualityLayer
    change: Literal[
        "new_failure",
        "fixed",
        "unchanged",
        "new_case",
        "removed",
        "not_evaluated",
        "not_comparable",
    ]


class GateDiff(QualityModel):
    case_id: NonEmptyStr
    change: Literal["new_failure", "fixed", "unchanged", "new_case", "removed"]
    previous_passed: bool | None
    current_passed: bool | None
    answer_diff: str
    previous_agent_version_id: UUID | None
    current_agent_version_id: UUID
    previous_run_ids: list[UUID]
    current_run_ids: list[UUID]
    previous_evidence_ids: list[NonEmptyStr]
    current_evidence_ids: list[NonEmptyStr]
    layer_changes: list[GateLayerChange]
    comparison_status: Literal[
        "comparable", "conditions_changed", "insufficient_evidence", "no_baseline"
    ]
    changed_conditions: list[str]
    previous_conditions_sha256: Sha256 | None
    current_conditions_sha256: Sha256 | None

    @field_validator("changed_conditions")
    @classmethod
    def _condition_pointers(cls, value: list[str]) -> list[str]:
        for pointer in value:
            pointer_tokens(pointer)
        return value

    @model_validator(mode="after")
    def _consistent_comparison(self) -> Self:
        ensure_unique(self.previous_evidence_ids, "previous evidence_id")
        ensure_unique(self.current_evidence_ids, "current evidence_id")
        layers = [item.layer for item in self.layer_changes]
        if set(layers) != set(QUALITY_LAYERS) or len(layers) != len(QUALITY_LAYERS):
            raise ValueError("layer_changes must contain each quality layer exactly once")
        if self.comparison_status == "conditions_changed" and not self.changed_conditions:
            raise ValueError("conditions_changed requires changed condition pointers")
        if self.comparison_status != "comparable" and any(
            item.change != "not_comparable" for item in self.layer_changes
        ):
            raise ValueError("non-comparable diffs must mark every layer not_comparable")
        expected_sides = {"new_failure": (True, False), "fixed": (False, True)}
        expected = expected_sides.get(self.change)
        if expected is not None and (self.previous_passed, self.current_passed) != expected:
            raise ValueError("diff change does not match previous/current pass states")
        if self.change == "new_case" and (
            self.previous_passed is not None or self.current_passed is None
        ):
            raise ValueError("new_case diff requires only a current pass state")
        if self.change == "unchanged" and (
            self.previous_passed is None or self.previous_passed != self.current_passed
        ):
            raise ValueError("unchanged diff requires equal non-null pass states")
        if self.change == "removed" and (
            self.previous_passed is None or self.current_passed is not None
        ):
            raise ValueError("removed diff requires only a previous pass state")
        return self


class GateRecordV1(QualityModel):
    id: UUID
    agent_version_id: UUID
    suite_id: UUID
    baseline_gate_id: UUID | None
    status: NonEmptyStr
    quality_record_version: Literal[1]
    conditions: GateConditions | None
    details: list[GateDetail]
    evidence: list[GateCaseEvidence] | None
    diff: list[GateDiff]

    @model_validator(mode="before")
    @classmethod
    def _no_secret_material(cls, value: Any) -> Any:
        if isinstance(value, dict):
            assert_public_safe(value)
        return value

    @model_validator(mode="after")
    def _internal_references(self) -> Self:
        if self.conditions is None:
            return self
        conditions = self.conditions
        if conditions.suite.suite_id != self.suite_id:
            raise ValueError("Gate conditions reference a different suite_id")
        if conditions.execution.agent_version_id != self.agent_version_id:
            raise ValueError("Gate conditions reference a different agent_version_id")
        evidence = self.evidence or []
        known_cases = set(conditions.suite.case_ids)
        ensure_unique([item.evidence_id for item in evidence], "Gate evidence_id")
        attempt_keys = [(item.case_id, item.attempt) for item in evidence]
        if len(attempt_keys) != len(set(attempt_keys)):
            raise ValueError("duplicate Gate case attempt")
        required = conditions.required_layers()
        for item in evidence:
            if item.case_id not in known_cases:
                raise ValueError("Gate evidence references a case outside the frozen suite")
            if item.agent_version_id != self.agent_version_id:
                raise ValueError("Gate evidence references a different AgentVersion")
            aggregated = aggregate_layers(item.layers.as_mapping(), required)
            if item.passed != aggregated.passed:
                raise ValueError("Gate evidence passed flag does not match required layers")
            if item.failed_layers != list(aggregated.failed_layers):
                raise ValueError("Gate evidence failed_layers does not match layer aggregation")
        for case_id in conditions.suite.case_ids:
            attempts = [item.attempt for item in evidence if item.case_id == case_id]
            if attempts != sorted(attempts):
                raise ValueError("Gate attempts must retain stable ascending order")
            if attempts and attempts != list(range(1, len(attempts) + 1)):
                raise ValueError("Gate attempts must be complete and contiguous from 1")
            if (
                self.status in {"passed", "failed"}
                and len(attempts) != conditions.execution.run_count
            ):
                raise ValueError("terminal Gate does not contain every frozen run_count attempt")
        self._validate_details(evidence)
        self._validate_diffs(evidence)
        return self

    def _validate_details(self, evidence: list[GateCaseEvidence]) -> None:
        by_case: dict[str, list[GateCaseEvidence]] = {}
        for item in evidence:
            by_case.setdefault(item.case_id, []).append(item)
        for detail in self.details:
            attempts = by_case.get(detail.case_id, [])
            run_ids = [item.run_id for item in attempts]
            if detail.run_ids is not None and detail.run_ids != run_ids:
                raise ValueError("Gate detail run_ids do not match ordered evidence attempts")
            if detail.attempt_count is not None and detail.attempt_count != len(attempts):
                raise ValueError("Gate detail attempt_count does not match evidence")
            if attempts:
                final = attempts[-1]
                if detail.passed != final.passed:
                    raise ValueError("Gate detail passed flag does not match final evidence")
                if detail.failed_layers is not None and detail.failed_layers != final.failed_layers:
                    raise ValueError("Gate detail failed_layers do not match final evidence")

    def _validate_diffs(self, evidence: list[GateCaseEvidence]) -> None:
        for diff in self.diff:
            if diff.current_agent_version_id != self.agent_version_id:
                raise ValueError("current diff side must equal the Gate AgentVersion")
            attempts = [item for item in evidence if item.case_id == diff.case_id]
            if diff.current_run_ids != [item.run_id for item in attempts]:
                raise ValueError("current diff run_ids do not match ordered Gate evidence")
            if diff.current_evidence_ids != [item.evidence_id for item in attempts]:
                raise ValueError("current diff evidence_ids do not match ordered Gate evidence")
            if attempts and diff.current_passed != attempts[-1].passed:
                raise ValueError("current diff pass state does not match final Gate evidence")


def validate_gate_references(gate: GateRecordV1, runs: Mapping[str, RunRecordV1]) -> None:
    """Resolve every assertion against the exact Run, never against similar answer text."""
    for evidence in gate.evidence or []:
        run = runs.get(str(evidence.run_id))
        if run is None:
            raise ValueError("Gate evidence references a missing Run")
        if run.agent_version_id != evidence.agent_version_id:
            raise ValueError("Gate evidence Run and AgentVersion do not match")
        document = run.model_dump(mode="json")
        answer_evidence = set()
        queries = set()
        calls = set()
        sources = set()
        if run.answer_output is not None:
            answer_evidence = {item.evidence_id for item in run.answer_output.evidence}
        if run.retrieval_output is not None:
            queries = {item.query_id for item in run.retrieval_output.queries}
            sources = {item.source_id for item in run.retrieval_output.sources}
        if run.tool_output is not None:
            calls = {item.call_id for item in run.tool_output.calls}
        known_ids = {
            "evidence_id": answer_evidence,
            "query_id": queries,
            "call_id": calls,
            "source_id": sources,
        }
        for layer in QUALITY_LAYERS:
            for assertion in getattr(evidence.layers, layer).assertions:
                subject = assertion.subject
                if subject.run_id != evidence.run_id:
                    raise ValueError("assertion subject run_id differs from its Gate evidence")
                tokens = pointer_tokens(subject.pointer)
                if tokens[0] != _LAYER_POINTERS[layer]:
                    raise ValueError("assertion pointer resolves to a different quality layer")
                try:
                    resolve_json_pointer(document, subject.pointer)
                except JsonPointerError as exc:
                    raise ValueError("assertion pointer does not resolve in the exact Run") from exc
                for field, identifiers in known_ids.items():
                    value = getattr(subject, field)
                    if value is not None and value not in identifiers:
                        raise ValueError(f"assertion subject has dangling {field}: {value}")


__all__ = [
    "AssertionSubject",
    "GateAssertion",
    "GateCaseEvidence",
    "GateCaseLayers",
    "GateCaseSnapshot",
    "GateComparisonConditions",
    "GateConditions",
    "GateDetail",
    "GateDiff",
    "GateExecutionConditions",
    "GateLayer",
    "GateLayerChange",
    "GateRecordV1",
    "GateRetrievalConditions",
    "GateScorer",
    "GateSuiteConditions",
    "LayerAggregation",
    "LayerStatus",
    "aggregate_layers",
    "validate_gate_references",
]

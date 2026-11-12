"""Build Gate v1 evidence and layer-aware diffs from exact Run references."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schema import public_legacy_dto
from app.models import GateRun, Run
from app.quality.conditions import canonical_sha256
from app.quality.gate import (
    QUALITY_LAYERS,
    GateCaseEvidence,
    GateConditions,
    GateLayer,
    GateRecordV1,
    aggregate_layers,
    validate_gate_references,
)
from app.quality.pointer import JsonPointerError, resolve_json_pointer
from app.quality.readers import read_run_record


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except (TypeError, ValueError):
        return fallback


def _run_payload(run: Run) -> dict[str, Any]:
    return {
        "id": run.id, "agent_id": run.agent_id, "agent_version_id": run.agent_version_id,
        "input": run.input, "output": run.output, "status": run.status,
        "quality_record_version": run.quality_record_version,
        "answer_output": _json(run.answer_output, None),
        "tool_output": _json(run.tool_output, None),
        "retrieval_output": _json(run.retrieval_output, None),
        "execution_output": _json(run.execution_output, None),
    }


def _layer(
    run_id: str, pointer: str, kind: str, status: str, reason: str,
    *, score: float | None = None, details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "assertions": [{
            "assertion_id": kind, "kind": kind, "status": status, "score": score,
            "reason": reason, "subject": {"run_id": run_id, "pointer": pointer},
            "rule_version": "v1", "details": details or {},
        }],
    }


def _not_evaluated() -> dict[str, Any]:
    return {"status": "not_evaluated", "assertions": []}


async def build_gate_evidence(
    session: AsyncSession, gate: GateRun, cases: list[Any], conditions: dict[str, Any],
) -> list[dict[str, Any]]:
    required = GateConditions.model_validate(conditions).required_layers()
    evidence: list[dict[str, Any]] = []
    for case in cases:
        if not case.run_id:
            raise ValueError(f"evaluation case {case.test_case_id} has no exact Run ID")
        run = await session.get(Run, case.run_id)
        if run is None or run.agent_version_id != gate.agent_version_id:
            raise ValueError(f"evaluation case {case.test_case_id} references an unavailable Run")
        read = read_run_record(_run_payload(run))
        if read.status != "v1" or read.record is None:
            raise ValueError(f"Run {run.id} has no supported four-layer observation record")
        observed = read.record
        output = _layer(
            run.id, "/answer_output", "output.case_score", "passed" if case.passed else "failed",
            "The frozen scorer accepted this answer." if case.passed else "The frozen scorer rejected this answer.",
            score=float(case.score), details=public_legacy_dto(case.score_details),
        ) if observed.answer_output is not None else _layer(
            run.id, "/answer_output", "output.observation", "not_available", "No answer observation was collected.",
        )
        execution = observed.execution_output
        system_passed = bool(
            execution and execution.result.record_complete and execution.error is None
            and run.status in {"success", "completed", "ok"}
        )
        system = _layer(
            run.id, "/execution_output/result", "system.terminal", "passed" if system_passed else "failed",
            "The Run reached a complete successful terminal state." if system_passed else "The Run failed or its execution record is incomplete.",
        ) if execution is not None else _layer(
            run.id, "/execution_output", "system.observation", "not_available", "No execution observation was collected.",
        )
        if "tool" not in required:
            tool = _not_evaluated()
        else:
            calls = observed.tool_output.calls if observed.tool_output else []
            results = observed.tool_output.results if observed.tool_output else []
            expected = case.expected_tool
            called = any(item.name == expected for item in calls) if expected else True
            passed = called and all(item.status == "success" for item in results)
            tool = _layer(
                run.id, "/tool_output", "tool.expectation", "passed" if passed else "failed",
                "Tool expectations and results passed." if passed else "An expected tool was missing or returned an error.",
                details={"expected_tool": expected, "called_tools": [item.name for item in calls]},
            )
        if "retrieval" not in required:
            retrieval = _not_evaluated()
        else:
            queries = observed.retrieval_output.queries if observed.retrieval_output else []
            passed = bool(queries) and all(item.status == "completed" for item in queries)
            retrieval = _layer(
                run.id, "/retrieval_output", "retrieval.completed", "passed" if passed else "failed",
                "Authorized retrieval completed; source relevance is judged separately." if passed else "No successful authorized retrieval was observed.",
                details={"query_count": len(queries)},
            )
        layers = {"output": output, "tool": tool, "retrieval": retrieval, "system": system}
        typed_layers = {name: GateLayer.model_validate(value) for name, value in layers.items()}
        aggregation = aggregate_layers(typed_layers, required)
        item = GateCaseEvidence.model_validate({
            "evidence_id": f"{gate.id}:{case.test_case_id}:1", "case_id": case.test_case_id,
            "result_case_id": case.case_id, "attempt": 1, "run_id": run.id,
            "agent_version_id": gate.agent_version_id, "passed": aggregation.passed,
            "failed_layers": list(aggregation.failed_layers), "layers": layers, "error": None,
        })
        evidence.append(item.model_dump(mode="json"))
    return evidence


def _changed_conditions(current: dict[str, Any], previous: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for pointer in current.get("comparison", {}).get("required_invariants", []):
        try:
            before = resolve_json_pointer(previous, pointer)
            after = resolve_json_pointer(current, pointer)
        except JsonPointerError:
            changed.append(pointer)
        else:
            if before != after:
                changed.append(pointer)
    return changed


def _layer_changes(
    change: str, comparison: str, previous: list[dict[str, Any]], current: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if comparison != "comparable":
        return [{"layer": layer, "change": "not_comparable"} for layer in QUALITY_LAYERS]
    if change in {"new_case", "removed"}:
        return [{"layer": layer, "change": change} for layer in QUALITY_LAYERS]
    before = previous[-1]["layers"] if previous else None
    after = current[-1]["layers"] if current else None
    output: list[dict[str, str]] = []
    for layer in QUALITY_LAYERS:
        if before is None or after is None:
            layer_change = "not_comparable"
        else:
            old_status = before[layer]["status"]
            new_status = after[layer]["status"]
            if "not_evaluated" in {old_status, new_status}:
                layer_change = "not_evaluated"
            elif old_status == "passed" and new_status != "passed":
                layer_change = "new_failure"
            elif old_status != "passed" and new_status == "passed":
                layer_change = "fixed"
            elif old_status == new_status:
                layer_change = "unchanged"
            else:
                layer_change = "not_comparable"
        output.append({"layer": layer, "change": layer_change})
    return output


def enrich_case_diffs(
    basic: list[dict[str, Any]], current_details: list[dict[str, Any]],
    previous_details: list[dict[str, Any]], gate: GateRun, baseline: GateRun | None,
    current_evidence: list[dict[str, Any]], previous_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_conditions = _json(gate.conditions, {})
    previous_conditions = _json(baseline.conditions, {}) if baseline else {}
    changed = _changed_conditions(current_conditions, previous_conditions) if baseline else []
    if baseline is None:
        comparison = "no_baseline"
    elif baseline.quality_record_version != 1 or not previous_evidence:
        comparison = "insufficient_evidence"
    elif changed:
        comparison = "conditions_changed"
    else:
        comparison = "comparable"
    old_details = {item["case_id"]: item for item in previous_details}
    new_details = {item["case_id"]: item for item in current_details}
    output: list[dict[str, Any]] = []
    for row in basic:
        case_id = row["case_id"]
        before = old_details.get(case_id, {})
        after = new_details.get(case_id, {})
        previous = [item for item in previous_evidence if item["case_id"] == case_id]
        current = [item for item in current_evidence if item["case_id"] == case_id]
        previous.sort(key=lambda item: item["attempt"])
        current.sort(key=lambda item: item["attempt"])
        status = comparison
        if comparison == "comparable" and row["change"] not in {"new_case", "removed"} and (not previous or not current):
            status = "insufficient_evidence"
        output.append({
            **row,
            "previous_agent_version_id": baseline.agent_version_id if baseline else None,
            "current_agent_version_id": gate.agent_version_id,
            "previous_run_ids": before.get("run_ids") or [item["run_id"] for item in previous],
            "current_run_ids": after.get("run_ids") or [item["run_id"] for item in current],
            "previous_evidence_ids": [item["evidence_id"] for item in previous],
            "current_evidence_ids": [item["evidence_id"] for item in current],
            "layer_changes": _layer_changes(row["change"], status, previous, current),
            "comparison_status": status, "changed_conditions": changed,
            "previous_conditions_sha256": canonical_sha256(previous_conditions) if baseline and previous_conditions else None,
            "current_conditions_sha256": canonical_sha256(current_conditions) if current_conditions else None,
        })
    return output


async def validate_gate_record(
    session: AsyncSession, gate: GateRun, details: list[dict[str, Any]],
    evidence: list[dict[str, Any]], diffs: list[dict[str, Any]],
) -> None:
    payload = {
        "id": gate.id, "agent_version_id": gate.agent_version_id, "suite_id": gate.suite_id,
        "baseline_gate_id": gate.baseline_gate_id, "status": gate.status,
        "quality_record_version": 1, "conditions": _json(gate.conditions, None),
        "details": details, "evidence": evidence, "diff": diffs,
    }
    record = GateRecordV1.model_validate(payload)
    runs = {}
    for item in evidence:
        run = await session.get(Run, item["run_id"])
        if run is None:
            raise ValueError("Gate evidence references a deleted Run")
        runs[run.id] = read_run_record(_run_payload(run)).record
    validate_gate_references(record, runs)


__all__ = ["build_gate_evidence", "enrich_case_diffs", "validate_gate_record"]

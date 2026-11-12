"""Pure quality evidence contracts and Reader compatibility (roadmap 16.10)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from app.api.schemas import RunCreate, RunDetailOut, RunOut, RunSummary
from app.quality.feedback import FeedbackRecord, FeedbackResolution, validate_feedback_resolution
from app.quality.gate import GateLayer, GateRecordV1, aggregate_layers, validate_gate_references
from app.quality.pointer import JsonPointerError, resolve_json_pointer
from app.quality.readers import read_gate_record, read_run_record
from app.quality.run import RunRecordV1
from pydantic import ValidationError

FIXTURES = Path(__file__).parents[2] / "docs" / "planning" / "quality_model"
RUN_ID = "33333333-3333-4333-8333-333333333333"
OTHER_RUN_ID = "33333333-3333-4333-8333-333333333334"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _run() -> dict:
    return _fixture("run-v1.json")


def _gate() -> dict:
    return _fixture("gate-v1.json")


def _validated_run(payload: dict) -> RunRecordV1:
    result = read_run_record(payload)
    assert result.status == "v1"
    assert result.record is not None
    return result.record


def _validated_gate(payload: dict) -> GateRecordV1:
    result = read_gate_record(payload)
    assert result.status == "v1"
    assert result.record is not None
    return result.record


def _add_tool(payload: dict) -> None:
    payload["tool_output"] = {
        "calls": [
            {
                "call_id": "call-1",
                "call_step_id": "tool-call-step",
                "name": "echo",
                "arguments": {"redacted": True, "paths": ["/api_key"]},
                "risk": "safe",
                "requested_at": "2026-11-10T10:00:00Z",
                "approval_id": None,
                "definition_version": "1",
            }
        ],
        "results": [
            {
                "call_id": "call-1",
                "result_step_id": "tool-result-step",
                "status": "success",
                "output": {"ok": True},
                "error": None,
                "duration_ms": 2,
                "finished_at": "2026-11-10T10:00:00.100Z",
            }
        ],
    }
    payload["execution_output"]["events"] = [
        {
            "event_id": "event-call",
            "step_id": "tool-call-step",
            "sequence": 1,
            "kind": "tool_call",
            "status": "ok",
            "parent_event_id": None,
        },
        {
            "event_id": "event-result",
            "step_id": "tool-result-step",
            "sequence": 2,
            "kind": "tool_result",
            "status": "ok",
            "parent_event_id": "event-call",
        },
        {**payload["execution_output"]["events"][0], "sequence": 3},
    ]
    payload["execution_output"]["metrics"]["tool_call_count"] = 1
    payload["execution_output"]["metrics"]["step_count"] = 3


def test_design_examples_form_an_exact_run_gate_feedback_chain() -> None:
    run_payload = _run()
    gate_payload = _gate()
    run = _validated_run(run_payload)
    gate = _validated_gate(gate_payload)
    validate_gate_references(gate, {str(run.id): run})
    feedback = FeedbackRecord.model_validate(_fixture("feedback-v1.json"))
    validate_feedback_resolution(feedback, feedback.resolution_links[0], gate, human_reviewed=False)
    assert gate.evidence[0].run_id == run.id
    assert gate.diff[0].current_run_ids == [run.id]
    assert feedback.run_id == run.id
    assert run.model_dump(mode="json")["answer_output"]["answer"] == run.output


def test_legacy_run_and_old_run_out_remain_compatible() -> None:
    payload = _fixture("legacy-run.json")
    original = copy.deepcopy(payload)
    result = read_run_record(payload)
    assert result.status == "legacy_unavailable"
    assert result.record is None
    assert result.raw == original == payload
    legacy = RunOut.model_validate(_run())
    assert legacy.output == _run()["output"]
    assert not hasattr(legacy, "answer_output")
    detail = RunDetailOut.model_validate(payload)
    assert detail.quality_record_version is None
    assert detail.answer_output is None
    v1_detail = RunDetailOut.model_validate(_run())
    assert v1_detail.quality_record_version == 1
    assert v1_detail.answer_output is not None
    assert v1_detail.answer_output.answer == v1_detail.output
    assert "answer_output" not in RunSummary.model_fields


@pytest.mark.parametrize("kind", ["run", "gate"])
def test_future_versions_are_preserved_but_never_scored(kind: str) -> None:
    payload = _run() if kind == "run" else _gate()
    payload["quality_record_version"] = 2
    payload["future_assertion"] = {"passed": True}
    original = copy.deepcopy(payload)
    reader = read_run_record if kind == "run" else read_gate_record
    result = reader(payload)
    assert result.status == "unsupported_version"
    assert result.record is None
    assert result.raw == original == payload


def test_v1_unknown_nested_keys_are_projected_without_rewriting_raw() -> None:
    payload = _run()
    payload["answer_output"]["future_nested"] = {"value": 1}
    result = read_run_record(payload)
    assert result.status == "v1"
    assert result.raw["answer_output"]["future_nested"] == {"value": 1}
    assert "future_nested" not in result.record.answer_output.model_dump()


def test_v1_missing_observation_key_is_not_treated_as_legacy() -> None:
    payload = _run()
    del payload["retrieval_output"]
    with pytest.raises(ValidationError):
        read_run_record(payload)


def test_client_cannot_inject_quality_objects_through_legacy_create_dto() -> None:
    request = RunCreate.model_validate(
        {
            "input": "question",
            "quality_record_version": 1,
            "answer_output": {"answer": "forged", "answer_status": "answered", "evidence": []},
        }
    )
    assert request.model_dump() == {
        "input": "question",
        "session_id": None,
        "agent_version_id": None,
        "model": None,
        "temperature": None,
        "max_steps": None,
        "async_run": False,
    }


def test_answer_text_and_run_support_observation_cannot_be_forged() -> None:
    mismatch = _run()
    mismatch["output"] = "different"
    with pytest.raises(ValidationError, match="exactly match"):
        read_run_record(mismatch)
    support = _run()
    support["answer_output"]["evidence"][0]["support_status"] = "supported"
    with pytest.raises(ValidationError):
        read_run_record(support)


@pytest.mark.parametrize("status", ["answered", "insufficient_evidence", "refused"])
def test_answer_disposition_is_not_a_gate_quality_verdict(status: str) -> None:
    payload = _run()
    payload["answer_status"] = status
    payload["answer_output"]["answer_status"] = status
    payload["answer_output"]["evidence"] = []
    record = _validated_run(payload)
    assert record.answer_output.answer_status == status
    gate = _validated_gate(_gate())
    assert gate.evidence[0].passed is False


def test_unicode_and_observed_empty_retrieval_round_trip() -> None:
    payload = _run()
    answer = "证据不足，无法确定。🚫"  # noqa: RUF001 -- intentional Unicode round-trip fixture.
    payload["output"] = answer
    payload["answer_output"] = {
        "answer": answer,
        "answer_status": "insufficient_evidence",
        "evidence": [],
    }
    payload["retrieval_output"] = {"queries": [], "sources": []}
    payload["execution_output"]["metrics"]["retrieval_query_count"] = 0
    record = _validated_run(payload)
    dumped = record.model_dump(mode="json")
    assert dumped["answer_output"]["answer"] == answer
    assert dumped["retrieval_output"] == {"queries": [], "sources": []}
    assert read_run_record(dumped).record == record


def test_complete_record_requires_layers_but_incomplete_may_be_partial() -> None:
    payload = _run()
    payload["answer_output"]["evidence"] = []
    payload["retrieval_output"] = None
    payload["execution_output"]["result"]["record_complete"] = False
    assert _validated_run(payload).retrieval_output is None
    payload["execution_output"]["result"]["record_complete"] = True
    with pytest.raises(ValidationError, match="requires all four"):
        read_run_record(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            lambda p: p["answer_output"]["evidence"].append(
                copy.deepcopy(p["answer_output"]["evidence"][0])
            ),
            id="duplicate-answer-evidence",
        ),
        pytest.param(
            lambda p: p["retrieval_output"]["queries"].append(
                copy.deepcopy(p["retrieval_output"]["queries"][0])
            ),
            id="duplicate-query",
        ),
        pytest.param(
            lambda p: p["retrieval_output"]["sources"].append(
                copy.deepcopy(p["retrieval_output"]["sources"][0])
            ),
            id="duplicate-source",
        ),
        pytest.param(
            lambda p: p["execution_output"]["events"].append(
                copy.deepcopy(p["execution_output"]["events"][0])
            ),
            id="duplicate-event",
        ),
    ],
)
def test_duplicate_observation_ids_are_rejected(mutation) -> None:
    payload = _run()
    mutation(payload)
    with pytest.raises(ValidationError):
        read_run_record(payload)


def test_answer_source_must_exist_be_injected_and_match_exact_excerpt() -> None:
    not_injected = _run()
    not_injected["retrieval_output"]["queries"][0]["candidates"][0]["injected"] = False
    with pytest.raises(ValidationError, match="injected source"):
        read_run_record(not_injected)
    missing = _run()
    missing["retrieval_output"]["queries"][0]["candidates"][0]["source_id"] = "missing"
    with pytest.raises(ValidationError, match="unknown source_id"):
        read_run_record(missing)
    excerpt = _run()
    excerpt["answer_output"]["evidence"][0]["source_excerpt"] = "not in the source"
    with pytest.raises(ValidationError, match="not present"):
        read_run_record(excerpt)


def test_tool_call_result_step_and_metric_references_are_checked() -> None:
    payload = _run()
    _add_tool(payload)
    record = _validated_run(payload)
    assert record.tool_output.results[0].call_id == "call-1"
    unknown_call = copy.deepcopy(payload)
    unknown_call["tool_output"]["results"][0]["call_id"] = "missing-call"
    with pytest.raises(ValidationError, match="unknown call_id"):
        read_run_record(unknown_call)
    unknown_step = copy.deepcopy(payload)
    unknown_step["tool_output"]["calls"][0]["call_step_id"] = "missing-step"
    with pytest.raises(ValidationError, match="unknown step_id"):
        read_run_record(unknown_step)
    bad_count = copy.deepcopy(payload)
    bad_count["execution_output"]["metrics"]["tool_call_count"] = 0
    with pytest.raises(ValidationError, match="tool_call_count"):
        read_run_record(bad_count)


def test_recursive_secret_material_is_rejected_even_below_unknown_keys() -> None:
    payload = _run()
    payload["future_extension"] = {"safe": [{"api_key": "plain-fixture-secret"}]}
    with pytest.raises(ValidationError, match="unsafe public value"):
        read_run_record(payload)
    tool = _run()
    _add_tool(tool)
    tool["tool_output"]["calls"][0]["arguments"] = {"nested": [{"password": "secret"}]}
    with pytest.raises(ValidationError, match="unsafe public value"):
        read_run_record(tool)


def test_execution_event_order_parent_time_and_tokens_are_checked() -> None:
    for field, value in [
        ("started_at", "2026-11-10T10:00:00"),
        ("finished_at", "2026-11-10T09:59:59Z"),
    ]:
        payload = _run()
        payload["execution_output"]["result"][field] = value
        with pytest.raises(ValidationError):
            read_run_record(payload)
    parent = _run()
    parent["execution_output"]["events"][0]["parent_event_id"] = "missing-parent"
    with pytest.raises(ValidationError, match="earlier event"):
        read_run_record(parent)
    tokens = _run()
    tokens["execution_output"]["metrics"]["total_tokens"] = 121
    with pytest.raises(ValidationError, match="total_tokens"):
        read_run_record(tokens)


def test_json_pointer_handles_rfc6901_escapes_empty_keys_and_root() -> None:
    document = {"a/b": {"~key": ["zero", {"": None}]}}
    assert resolve_json_pointer(document, "") is document
    assert resolve_json_pointer(document, "/a~1b/~0key/0") == "zero"
    assert resolve_json_pointer(document, "/a~1b/~0key/1/") is None


@pytest.mark.parametrize(
    "pointer",
    ["relative", "/a~2b", "/a~1b/~0key/-", "/a~1b/~0key/01", "/a~1b/~0key/9"],
)
def test_invalid_or_dangling_json_pointers_fail_closed(pointer: str) -> None:
    with pytest.raises(JsonPointerError):
        resolve_json_pointer({"a/b": {"~key": ["zero"]}}, pointer)


@pytest.mark.parametrize("status", ["failed", "not_evaluated", "not_available", "error"])
def test_every_required_non_passed_layer_blocks_case(status: str) -> None:
    layers = {
        name: GateLayer(status="passed", assertions=[])
        for name in ("output", "tool", "retrieval", "system")
    }
    layers["output"] = GateLayer(status=status, assertions=[])
    result = aggregate_layers(layers, ["output"])
    assert result.passed is False
    assert "output" in result.failed_layers


def test_optional_not_evaluated_is_visible_and_does_not_block() -> None:
    layers = {
        name: GateLayer(status="passed", assertions=[])
        for name in ("output", "tool", "retrieval", "system")
    }
    layers["tool"] = GateLayer(status="not_evaluated", assertions=[])
    result = aggregate_layers(layers, ["output", "retrieval", "system"])
    assert result.passed is True
    assert result.failed_layers == ()
    assert result.not_evaluated_layers == ("tool",)


def test_missing_required_layer_is_not_available_not_passed() -> None:
    result = aggregate_layers({}, ["system"])
    assert result.passed is False
    assert result.failed_layers == ("output", "tool", "retrieval", "system")


def test_legacy_gate_details_and_diff_are_returned_without_layer_scoring() -> None:
    payload = _gate()
    payload["quality_record_version"] = None
    payload["conditions"] = None
    payload["evidence"] = None
    original = copy.deepcopy(payload)
    result = read_gate_record(payload)
    assert result.status == "legacy_unavailable"
    assert result.record is None
    assert result.raw["details"] == original["details"]
    assert result.raw["diff"] == original["diff"]
    assert result.raw == payload


def test_gate_rejects_duplicate_noncontiguous_or_missing_attempts() -> None:
    duplicate = _gate()
    duplicate_attempt = copy.deepcopy(duplicate["evidence"][0])
    duplicate_attempt["evidence_id"] = "another-evidence-id"
    duplicate["evidence"].append(duplicate_attempt)
    with pytest.raises(ValidationError, match="duplicate Gate case attempt"):
        read_gate_record(duplicate)
    noncontiguous = _gate()
    noncontiguous["evidence"][0]["attempt"] = 2
    with pytest.raises(ValidationError):
        read_gate_record(noncontiguous)
    missing = _gate()
    missing["evidence"] = []
    with pytest.raises(ValidationError, match="run_count"):
        read_gate_record(missing)


def test_multiple_attempts_keep_stable_order_and_exact_run_links() -> None:
    gate_payload = _gate()
    second_run_payload = _run()
    second_run_payload["id"] = OTHER_RUN_ID
    second = copy.deepcopy(gate_payload["evidence"][0])
    second["evidence_id"] = "gate-evidence-2"
    second["attempt"] = 2
    second["run_id"] = OTHER_RUN_ID
    for layer in second["layers"].values():
        for assertion in layer["assertions"]:
            assertion["subject"]["run_id"] = OTHER_RUN_ID
    gate_payload["conditions"]["execution"]["run_count"] = 2
    gate_payload["evidence"].append(second)
    gate_payload["details"][0]["run_ids"].append(OTHER_RUN_ID)
    gate_payload["details"][0]["attempt_count"] = 2
    gate_payload["diff"][0]["current_run_ids"].append(OTHER_RUN_ID)
    gate_payload["diff"][0]["current_evidence_ids"].append("gate-evidence-2")
    gate = _validated_gate(gate_payload)
    first_run = _validated_run(_run())
    second_run = _validated_run(second_run_payload)
    validate_gate_references(gate, {str(first_run.id): first_run, str(second_run.id): second_run})
    assert [item.attempt for item in gate.evidence] == [1, 2]
    assert gate.diff[0].current_run_ids == [first_run.id, second_run.id]


def test_gate_pointers_resolve_against_exact_run_not_similar_text() -> None:
    run = _validated_run(_run())
    dangling = _gate()
    dangling["evidence"][0]["layers"]["retrieval"]["assertions"][0]["subject"]["pointer"] = (
        "/retrieval_output/queries/0/candidates/99"
    )
    with pytest.raises(ValueError, match="does not resolve"):
        validate_gate_references(_validated_gate(dangling), {str(run.id): run})
    wrong_layer = _gate()
    wrong_layer["evidence"][0]["layers"]["output"]["assertions"][0]["subject"]["pointer"] = (
        "/retrieval_output/queries/0/candidates/0"
    )
    with pytest.raises(ValueError, match="different quality layer"):
        validate_gate_references(_validated_gate(wrong_layer), {str(run.id): run})
    other = _validated_run({**_run(), "id": OTHER_RUN_ID})
    with pytest.raises(ValueError, match="missing Run"):
        validate_gate_references(_validated_gate(_gate()), {str(other.id): other})


def test_gate_optional_subject_ids_cannot_dangle() -> None:
    payload = _gate()
    subject = payload["evidence"][0]["layers"]["retrieval"]["assertions"][0]["subject"]
    subject["source_id"] = "missing-source"
    run = _validated_run(_run())
    with pytest.raises(ValueError, match="dangling source_id"):
        validate_gate_references(_validated_gate(payload), {str(run.id): run})


def test_gate_pass_and_failed_layers_must_match_aggregation() -> None:
    payload = _gate()
    payload["evidence"][0]["passed"] = True
    with pytest.raises(ValidationError, match="passed flag"):
        read_gate_record(payload)


@pytest.mark.parametrize(("field", "value"), [("passed", True), ("failed_layers", [])])
def test_legacy_gate_detail_cannot_contradict_final_evidence(field: str, value: object) -> None:
    payload = _gate()
    payload["details"][0][field] = value
    with pytest.raises(ValidationError, match="Gate detail"):
        read_gate_record(payload)


def test_legacy_diff_cannot_contradict_current_attempt() -> None:
    payload = _gate()
    payload["diff"][0].update({"change": "fixed", "previous_passed": False, "current_passed": True})
    with pytest.raises(ValidationError, match="current diff pass state"):
        read_gate_record(payload)


@pytest.mark.parametrize("status", ["conditions_changed", "insufficient_evidence", "no_baseline"])
def test_non_comparable_diff_cannot_claim_fixed_or_unchanged_layers(status: str) -> None:
    payload = _gate()
    diff = payload["diff"][0]
    diff["comparison_status"] = status
    diff["layer_changes"][0]["change"] = "fixed"
    if status == "conditions_changed":
        diff["changed_conditions"] = ["/execution/model"]
    if status == "no_baseline":
        diff.update({"change": "new_case", "previous_passed": None})
    with pytest.raises(ValidationError, match="not_comparable"):
        read_gate_record(payload)


def test_non_comparable_total_diff_still_preserves_legacy_change() -> None:
    payload = _gate()
    payload["diff"][0]["comparison_status"] = "conditions_changed"
    payload["diff"][0]["changed_conditions"] = ["/execution/model"]
    record = _validated_gate(payload)
    assert record.diff[0].change == "new_failure"
    assert {item.change for item in record.diff[0].layer_changes} == {"not_comparable"}


@pytest.mark.parametrize(
    ("change", "previous", "current"),
    [("new_failure", False, False), ("fixed", True, True), ("unchanged", None, None)],
)
def test_total_diff_change_must_match_pass_states(
    change: str, previous: bool | None, current: bool | None
) -> None:
    payload = _gate()
    payload["diff"][0].update(
        {"change": change, "previous_passed": previous, "current_passed": current}
    )
    with pytest.raises(ValidationError, match="diff"):
        read_gate_record(payload)


def test_changed_conditions_require_valid_nonempty_json_pointers() -> None:
    missing = _gate()
    missing["diff"][0]["comparison_status"] = "conditions_changed"
    missing["diff"][0]["changed_conditions"] = []
    with pytest.raises(ValidationError, match="changed condition pointers"):
        read_gate_record(missing)
    invalid = _gate()
    invalid["diff"][0]["changed_conditions"] = ["execution.model"]
    with pytest.raises(ValidationError):
        read_gate_record(invalid)


def _fixed_gate() -> GateRecordV1:
    payload = _gate()
    evidence = payload["evidence"][0]
    evidence["passed"] = True
    evidence["failed_layers"] = []
    for layer in evidence["layers"].values():
        layer["status"] = "passed"
        for assertion in layer["assertions"]:
            assertion["status"] = "passed"
    payload["details"][0]["passed"] = True
    payload["details"][0]["failed_layers"] = []
    payload["details"][0]["score"] = 1.0
    diff = payload["diff"][0]
    diff.update(
        {
            "change": "fixed",
            "previous_passed": False,
            "current_passed": True,
            "previous_evidence_ids": ["previous-gate-evidence"],
            "previous_conditions_sha256": "6" * 64,
            "comparison_status": "comparable",
        }
    )
    for layer_change in diff["layer_changes"]:
        layer_change["change"] = (
            "fixed" if layer_change["layer"] in {"output", "retrieval"} else "unchanged"
        )
    return _validated_gate(payload)


def test_confirmed_fixed_needs_explicit_human_review_and_passing_attempts() -> None:
    feedback_payload = _fixture("feedback-v1.json")
    feedback_payload.update(
        {
            "run_id": "44444444-4444-4444-8444-444444444444",
            "agent_version_id": "11111111-1111-4111-8111-111111111111",
        }
    )
    feedback = FeedbackRecord.model_validate(feedback_payload)
    resolution = FeedbackResolution.model_validate(
        {
            **_fixture("feedback-v1.json")["resolution_links"][0],
            "verdict": "confirmed_fixed",
            "note": "Reviewed the exact diff and source evidence.",
        }
    )
    gate = _fixed_gate()
    with pytest.raises(ValueError, match="authorized human"):
        validate_feedback_resolution(feedback, resolution, gate, human_reviewed=False)
    validate_feedback_resolution(feedback, resolution, gate, human_reviewed=True)
    with pytest.raises(ValueError, match="comparable fixed diff"):
        validate_feedback_resolution(
            feedback, resolution, _validated_gate(_gate()), human_reviewed=True
        )


def test_feedback_resolution_requires_exact_diff_run_and_agent_version() -> None:
    payload = _fixture("feedback-v1.json")
    resolution = FeedbackRecord.model_validate(payload).resolution_links[0]
    gate = _validated_gate(_gate())
    payload["run_id"] = OTHER_RUN_ID
    with pytest.raises(ValueError, match="feedback Run is absent"):
        validate_feedback_resolution(
            FeedbackRecord.model_validate(payload), resolution, gate, human_reviewed=False
        )
    payload = _fixture("feedback-v1.json")
    payload["agent_version_id"] = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(ValueError, match="feedback AgentVersion"):
        validate_feedback_resolution(
            FeedbackRecord.model_validate(payload), resolution, gate, human_reviewed=False
        )


def test_confirmed_fixed_cannot_reuse_the_current_passing_run_as_original_feedback() -> None:
    feedback = FeedbackRecord.model_validate(_fixture("feedback-v1.json"))
    resolution = FeedbackResolution.model_validate(
        {**feedback.resolution_links[0].model_dump(), "verdict": "confirmed_fixed"}
    )
    with pytest.raises(ValueError, match="original negative feedback Run"):
        validate_feedback_resolution(feedback, resolution, _fixed_gate(), human_reviewed=True)


def test_feedback_resolution_cannot_cross_regression_suites() -> None:
    feedback_payload = _fixture("feedback-v1.json")
    feedback_payload["suite_id"] = "44444444-4444-4444-8444-444444444445"
    feedback = FeedbackRecord.model_validate(feedback_payload)
    with pytest.raises(ValueError, match="regression suite"):
        validate_feedback_resolution(
            feedback, feedback.resolution_links[0], _validated_gate(_gate()), human_reviewed=False
        )


def test_gate_conditions_reject_secret_refs_even_as_unknown_fields() -> None:
    payload = _gate()
    payload["conditions"]["execution"]["secret_refs"] = [{"token": "plain-secret"}]
    with pytest.raises(ValidationError, match="unsafe public value"):
        read_gate_record(payload)

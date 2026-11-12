"""Append-only feedback resolution contracts and pure cross-record checks."""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.agent.schema import assert_public_safe
from app.quality.common import NonEmptyStr, QualityModel, UtcDatetime
from app.quality.gate import GateRecordV1


class FeedbackResolution(QualityModel):
    id: UUID
    feedback_id: UUID
    gate_run_id: UUID
    case_id: NonEmptyStr
    verdict: Literal["needs_review", "confirmed_fixed", "not_fixed", "conditions_changed"]
    note: str = Field(min_length=1)
    created_by: NonEmptyStr
    created_at: UtcDatetime


class FeedbackRecord(QualityModel):
    id: UUID
    run_id: UUID
    agent_id: UUID
    agent_version_id: UUID | None
    rating: Literal["positive", "negative"]
    expected_answer: str | None = None
    eval_case_id: NonEmptyStr | None = None
    suite_id: UUID | None = None
    resolution_links: list[FeedbackResolution] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _no_secret_material(cls, value: Any) -> Any:
        if isinstance(value, dict):
            assert_public_safe(value)
        return value

    @model_validator(mode="after")
    def _links_belong_to_feedback(self) -> Self:
        for link in self.resolution_links:
            if link.feedback_id != self.id:
                raise ValueError("resolution link belongs to a different feedback record")
        return self


def validate_feedback_resolution(
    feedback: FeedbackRecord,
    resolution: FeedbackResolution,
    gate: GateRecordV1,
    *,
    human_reviewed: bool,
) -> None:
    """Validate links; ``confirmed_fixed`` always requires an explicit human review fact."""
    if resolution.feedback_id != feedback.id or resolution.gate_run_id != gate.id:
        raise ValueError("resolution does not reference the supplied feedback and GateRun")
    diff = next((item for item in gate.diff if item.case_id == resolution.case_id), None)
    if diff is None:
        raise ValueError("resolution case_id is absent from the GateRun diff")
    if feedback.eval_case_id is not None and feedback.eval_case_id != resolution.case_id:
        raise ValueError("resolution case_id does not match the feedback regression case")
    if feedback.suite_id is not None and feedback.suite_id != gate.suite_id:
        raise ValueError("resolution GateRun does not match the feedback regression suite")
    expected_versions = set()
    if feedback.run_id in diff.previous_run_ids:
        expected_versions.add(diff.previous_agent_version_id)
    if feedback.run_id in diff.current_run_ids:
        expected_versions.add(diff.current_agent_version_id)
    if not expected_versions:
        raise ValueError("feedback Run is absent from the resolution Gate diff")
    if feedback.agent_version_id not in expected_versions:
        raise ValueError("feedback AgentVersion does not match its Run's Gate diff side")
    if resolution.verdict != "confirmed_fixed":
        return
    if feedback.rating != "negative" or feedback.run_id not in diff.previous_run_ids:
        raise ValueError(
            "confirmed_fixed requires the original negative feedback Run on the baseline side"
        )
    if not human_reviewed:
        raise ValueError("confirmed_fixed requires authorized human review")
    if not resolution.note.strip():
        raise ValueError("confirmed_fixed requires a human review reason")
    if diff.comparison_status != "comparable" or diff.change != "fixed":
        raise ValueError("confirmed_fixed requires a comparable fixed diff")
    if diff.current_passed is not True:
        raise ValueError("confirmed_fixed requires the current case to pass")
    current = [item for item in gate.evidence or [] if item.case_id == resolution.case_id]
    if not current or any(not item.passed for item in current):
        raise ValueError("confirmed_fixed requires every current attempt to pass")


__all__ = ["FeedbackRecord", "FeedbackResolution", "validate_feedback_resolution"]

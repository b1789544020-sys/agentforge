"""Run v1 raw observations; these models never contain Gate pass/fail judgments."""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.agent.schema import assert_public_safe
from app.quality.common import (
    NonEmptyStr,
    QualityError,
    QualityModel,
    Sha256,
    UtcDatetime,
    ensure_unique,
)


class AnswerEvidence(QualityModel):
    evidence_id: NonEmptyStr
    source_id: NonEmptyStr
    citation_text: str
    answer_excerpt: str
    source_excerpt: str
    support_status: Literal["not_checked"]


class AnswerOutput(QualityModel):
    answer: str | None
    answer_status: Literal[
        "answered", "insufficient_evidence", "refused", "not_produced", "unknown"
    ]
    evidence: list[AnswerEvidence]

    @model_validator(mode="after")
    def _unique_evidence(self) -> Self:
        ensure_unique([item.evidence_id for item in self.evidence], "answer evidence_id")
        if self.answer is None and self.evidence:
            raise ValueError("answer evidence requires answer text")
        if self.answer is not None:
            for item in self.evidence:
                if item.answer_excerpt not in self.answer or item.citation_text not in self.answer:
                    raise ValueError("answer evidence excerpts must occur in the answer")
        return self


class ToolCall(QualityModel):
    call_id: NonEmptyStr
    call_step_id: NonEmptyStr | None
    name: NonEmptyStr
    arguments: Any
    risk: NonEmptyStr
    requested_at: UtcDatetime
    approval_id: NonEmptyStr | None
    definition_version: NonEmptyStr | None


class ToolResult(QualityModel):
    call_id: NonEmptyStr
    result_step_id: NonEmptyStr | None
    status: Literal["success", "error", "cancelled", "timeout", "denied"]
    output: Any
    error: QualityError | None
    duration_ms: int = Field(ge=0)
    finished_at: UtcDatetime

    @model_validator(mode="after")
    def _error_matches_status(self) -> Self:
        if self.status == "success" and self.error is not None:
            raise ValueError("successful tool result cannot contain an error")
        if self.status != "success" and self.error is None:
            raise ValueError("unsuccessful tool result requires a structured error")
        return self


class ToolOutput(QualityModel):
    calls: list[ToolCall]
    results: list[ToolResult]

    @model_validator(mode="after")
    def _references(self) -> Self:
        call_ids = [item.call_id for item in self.calls]
        ensure_unique(call_ids, "tool call_id")
        result_ids = [item.call_id for item in self.results]
        ensure_unique(result_ids, "tool result call_id")
        unknown = set(result_ids) - set(call_ids)
        if unknown:
            raise ValueError(f"tool result references unknown call_id: {sorted(unknown)[0]}")
        return self


class EmbeddingObservation(QualityModel):
    provider: NonEmptyStr
    model: NonEmptyStr
    dimensions: int = Field(ge=1)


class IndexObservation(QualityModel):
    snapshot_id: NonEmptyStr
    algorithm: NonEmptyStr
    corpus_manifest_sha256: Sha256


class RetrievalCandidate(QualityModel):
    source_id: NonEmptyStr
    rank: int = Field(ge=1)
    score: float
    injected: bool


class RetrievalQuery(QualityModel):
    query_id: NonEmptyStr
    text: str
    knowledge_base_ids: list[NonEmptyStr]
    embedding: EmbeddingObservation
    index: IndexObservation
    top_k: int = Field(ge=1)
    filters: dict[str, Any]
    candidates: list[RetrievalCandidate]
    duration_ms: int = Field(ge=0)
    status: Literal["completed", "error", "cancelled"]
    error: QualityError | None

    @model_validator(mode="after")
    def _references(self) -> Self:
        ensure_unique(self.knowledge_base_ids, "query knowledge_base_id")
        ensure_unique([item.source_id for item in self.candidates], "query candidate source_id")
        ranks = [item.rank for item in self.candidates]
        if ranks != sorted(ranks) or len(ranks) != len(set(ranks)):
            raise ValueError("retrieval candidates must have unique ascending ranks")
        if self.status == "completed" and self.error is not None:
            raise ValueError("completed retrieval query cannot contain an error")
        if self.status == "error" and self.error is None:
            raise ValueError("error retrieval query requires a structured error")
        return self


class SourceLocator(QualityModel):
    filename: NonEmptyStr
    section: NonEmptyStr | None


class RetrievalSource(QualityModel):
    source_id: NonEmptyStr
    knowledge_base_id: NonEmptyStr
    document_id: NonEmptyStr
    chunk_id: NonEmptyStr
    document_sha256: Sha256
    chunk_sha256: Sha256
    locator: SourceLocator
    source_excerpt: str
    excerpt_truncated: bool


class RetrievalOutput(QualityModel):
    queries: list[RetrievalQuery]
    sources: list[RetrievalSource]

    @model_validator(mode="after")
    def _unique_observations(self) -> Self:
        ensure_unique([item.query_id for item in self.queries], "retrieval query_id")
        ensure_unique([item.source_id for item in self.sources], "retrieval source_id")
        return self


class ExecutionEvent(QualityModel):
    event_id: NonEmptyStr
    step_id: NonEmptyStr | None
    sequence: int = Field(ge=1)
    kind: NonEmptyStr
    status: NonEmptyStr
    parent_event_id: NonEmptyStr | None


class ExecutionMetrics(QualityModel):
    duration_ms: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    step_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    retrieval_query_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _tokens_add_up(self) -> Self:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total_tokens must equal prompt_tokens + completion_tokens")
        return self


class ExecutionResult(QualityModel):
    status: NonEmptyStr
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    termination_reason: str | None
    record_complete: bool

    @model_validator(mode="after")
    def _consistent_terminal_state(self) -> Self:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("execution finished_at cannot precede started_at")
        if self.record_complete and self.finished_at is None:
            raise ValueError("complete execution records require finished_at")
        return self


class ExecutionOutput(QualityModel):
    events: list[ExecutionEvent]
    metrics: ExecutionMetrics
    error: QualityError | None
    result: ExecutionResult

    @model_validator(mode="after")
    def _event_graph(self) -> Self:
        ensure_unique([item.event_id for item in self.events], "execution event_id")
        sequences = [item.sequence for item in self.events]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("execution events must have unique ascending sequences")
        earlier: set[str] = set()
        for event in self.events:
            if event.parent_event_id is not None and event.parent_event_id not in earlier:
                raise ValueError("execution parent_event_id must reference an earlier event")
            earlier.add(event.event_id)
        return self


class RunObservationsV1(QualityModel):
    quality_record_version: Literal[1]
    answer_output: AnswerOutput | None
    tool_output: ToolOutput | None
    retrieval_output: RetrievalOutput | None
    execution_output: ExecutionOutput | None

    @model_validator(mode="before")
    @classmethod
    def _no_secret_material(cls, value: Any) -> Any:
        if isinstance(value, dict):
            assert_public_safe(value)
        return value

    @model_validator(mode="after")
    def _cross_references(self) -> Self:
        if self.execution_output is not None:
            execution = self.execution_output
            if execution.result.record_complete and any(
                layer is None
                for layer in (
                    self.answer_output,
                    self.tool_output,
                    self.retrieval_output,
                    self.execution_output,
                )
            ):
                raise ValueError("record_complete=true requires all four observation objects")
            if self.tool_output is not None:
                actual = len(self.tool_output.calls)
                if execution.metrics.tool_call_count != actual:
                    raise ValueError("tool_call_count does not match tool observations")
            if self.retrieval_output is not None:
                actual = len(self.retrieval_output.queries)
                if execution.metrics.retrieval_query_count != actual:
                    raise ValueError("retrieval_query_count does not match retrieval observations")
            known_steps = {event.step_id for event in execution.events if event.step_id is not None}
            observed_steps: list[str] = []
            if self.tool_output is not None:
                observed_steps.extend(
                    item.call_step_id for item in self.tool_output.calls if item.call_step_id
                )
                observed_steps.extend(
                    item.result_step_id for item in self.tool_output.results if item.result_step_id
                )
            if execution.error is not None and execution.error.step_id:
                observed_steps.append(execution.error.step_id)
            unknown_steps = set(observed_steps) - known_steps
            if unknown_steps:
                raise ValueError(
                    f"observation references unknown step_id: {sorted(unknown_steps)[0]}"
                )

        if self.retrieval_output is not None:
            sources = {item.source_id: item for item in self.retrieval_output.sources}
            injected: set[str] = set()
            for query in self.retrieval_output.queries:
                for candidate in query.candidates:
                    source = sources.get(candidate.source_id)
                    if source is None:
                        raise ValueError("retrieval candidate references unknown source_id")
                    if source.knowledge_base_id not in query.knowledge_base_ids:
                        raise ValueError("retrieval candidate is outside the query KB scope")
                    if candidate.injected:
                        injected.add(candidate.source_id)
        else:
            sources = {}
            injected = set()

        if self.answer_output is not None:
            for evidence in self.answer_output.evidence:
                source = sources.get(evidence.source_id)
                if source is None or evidence.source_id not in injected:
                    raise ValueError("answer evidence must reference an injected source")
                if evidence.source_excerpt not in source.source_excerpt:
                    raise ValueError("answer evidence excerpt is not present in its source")
        return self


class RunRecordV1(RunObservationsV1):
    id: UUID
    agent_id: UUID
    agent_version_id: UUID | None
    input: str
    output: str | None
    status: NonEmptyStr

    @model_validator(mode="after")
    def _answer_matches_legacy_output(self) -> Self:
        if (
            self.answer_output is not None
            and self.answer_output.answer is not None
            and self.answer_output.answer != self.output
        ):
            raise ValueError("answer_output.answer must exactly match Run.output")
        return self


__all__ = [
    "AnswerEvidence",
    "AnswerOutput",
    "EmbeddingObservation",
    "ExecutionEvent",
    "ExecutionMetrics",
    "ExecutionOutput",
    "ExecutionResult",
    "IndexObservation",
    "RetrievalCandidate",
    "RetrievalOutput",
    "RetrievalQuery",
    "RetrievalSource",
    "RunObservationsV1",
    "RunRecordV1",
    "SourceLocator",
    "ToolCall",
    "ToolOutput",
    "ToolResult",
]

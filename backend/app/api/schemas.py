"""Pydantic schemas shared across the API layer."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field


class OrmModel(BaseModel):
    """Base for ORM-backed schemas — allows ``from_attributes`` style access."""

    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = None
    system_prompt: str | None = None
    model: str = "gpt-4o-mini"
    provider: str = "openai"
    temperature: float = 0.7
    max_steps: int = 20
    tools: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    provider: str | None = None
    temperature: float | None = None
    max_steps: int | None = None
    tools: list[str] | None = None
    config: dict[str, Any] | None = None


class AgentOut(OrmModel):
    id: str
    name: str
    description: str | None = None
    system_prompt: str | None = None
    model: str
    provider: str
    temperature: float
    max_steps: int
    tools: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
class RunCreate(BaseModel):
    input: str = Field(..., min_length=1)
    session_id: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_steps: int | None = None
    async_run: bool = False


class StepOut(OrmModel):
    id: str
    index: int
    kind: str
    name: str | None = None
    content: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = 0
    tokens: int = 0
    status: str = "ok"
    error: str | None = None
    created_at: datetime | None = None

    # 0.1.3 #9 — risk class for tool_call steps.  Surfaced from
    # ``payload["risk"]`` (which the agent loop sets via
    # ``ToolRegistry.spec(name).risk``) so the frontend ``StepNode``
    # can render a coloured warning badge.  ``None`` for non-tool
    # steps (think / final_answer / observation / error) or for
    # legacy step rows that pre-date the field.
    @computed_field  # type: ignore[misc]
    @property
    def risk(self) -> str | None:  # noqa: D401 - tiny helper
        """Tool risk class (``safe`` / ``read`` / ``write`` / ``network``
        / ``exec``) for ``tool_call`` steps, otherwise ``None``."""
        if self.kind != "tool_call":
            return None
        if not isinstance(self.payload, dict):
            return None
        value = self.payload.get("risk")
        return value if isinstance(value, str) and value else None


class RunOut(OrmModel):
    id: str
    agent_id: str
    input: str
    output: str | None = None
    status: str
    error: str | None = None
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    step_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)


class RunSummary(OrmModel):
    id: str
    agent_id: str
    input: str
    output: str | None = None
    status: str
    total_tokens: int = 0
    duration_ms: int = 0
    step_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None


# ---------------------------------------------------------------------------
# Evals
# ---------------------------------------------------------------------------
class EvalCaseIn(BaseModel):
    """A single eval case provided inline with the request."""

    input: str
    expected: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalCreate(BaseModel):
    """Create + run an evaluation.

    Two modes are supported:

    1. **Inline** (legacy): provide ``cases`` as a list of
       :class:`EvalCaseIn` items.  The API will materialise them as
       :class:`app.models.EvalCase` rows in memory and run them through
       the agent immediately.
    2. **Dataset refs** (new): provide ``test_case_ids`` (a list of
       existing :class:`app.models.EvalCase` ids) and optionally a
       ``scorer_name`` from the registry in
       :mod:`app.eval.scorer`.  The runner will execute those cases and
       persist per-case result rows.
    """

    dataset_name: str = "default"
    scorer: str | None = None
    scorer_name: str | None = None
    pass_threshold: float = 0.7
    cases: list[EvalCaseIn] = Field(default_factory=list)
    test_case_ids: list[str] = Field(default_factory=list)
    # 0.1.2 5.1 轮: 真并发. 默认 4 (= 4 核 CPU 一次跑 4 个 case).
    # 调高会撑爆 LLM 速率; 设为 1 = 串行 (兼容 0.1.0 行为).
    max_concurrency: int = 4

    @property
    def is_dataset_ref(self) -> bool:  # noqa: D401 - tiny helper
        return bool(self.test_case_ids)


class EvalCaseOut(OrmModel):
    id: str
    index: int
    input: str
    expected: Any | None = None
    output: str | None = None
    score: float = 0.0
    passed: bool = False
    duration_ms: int = 0
    tokens: int = 0
    error: str | None = None
    # New: full per-case tool + score breakdown for the UI.
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    score_details: dict[str, Any] = Field(default_factory=dict)


class EvalProgressOut(BaseModel):
    """Snapshot of an in-flight evaluation.

    ``progress`` is a *derived* quantity — it is always computed from
    ``done`` and ``total`` (or forced to ``1.0`` when ``status`` is
    ``"completed"``).  Accepting it in the constructor would be a foot-
    gun because two callers could disagree on the value; the property
    below is the single source of truth.
    """

    eval_run_id: str
    status: str
    total: int
    passed: int
    failed: int
    done: int
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @computed_field  # type: ignore[misc]
    @property
    def progress(self) -> float:  # noqa: D401 - tiny helper
        """Fraction of cases done (0..1).  Always 1.0 when finished."""
        if self.status == "completed":
            return 1.0
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.done / self.total))


class EvalRunOut(OrmModel):
    id: str
    agent_id: str
    dataset: str
    scorer: str | None = None
    pass_threshold: float = 0.7
    total: int
    passed: int
    failed: int
    average_score: float
    duration_ms: int = 0
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cases: list[EvalCaseOut] = Field(default_factory=list)
    # Optional legacy ``results`` blob (kept for back-compat with the
    # original code path that writes raw results here).
    results: list[dict[str, Any]] = Field(default_factory=list)


class EvalUploadResponse(BaseModel):
    """Response for ``POST /evals/upload`` (0.1.3 #2)."""

    run_id: str
    dataset_name: str
    case_count: int
    accepted: int
    # Number of lines that were rejected by the parser.  Always 0 on
    # the happy path; non-zero only when a malformed file was sent
    # *after* a successful partial parse (currently the endpoint
    # aborts on the first batch of errors, but the field is here so
    # future partial-accept semantics can be expressed without
    # changing the response shape).
    rejected: int = 0
    format: str
    duration_ms: int = 0
    average_score: float = 0.0
    passed: int = 0
    failed: int = 0
    # IDs of the freshly persisted :class:`app.models.EvalCase` rows.
    # Always present even on a fully successful run so the client can
    # open the detail page without a second round-trip.
    case_ids: list[str] = Field(default_factory=list)



# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
class MemoryIn(BaseModel):
    content: str
    kind: str = "episodic"
    importance: float = 0.5
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryOut(OrmModel):
    id: str
    agent_id: str | None = None
    content: str
    kind: str
    importance: float
    created_at: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
class HealthOut(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    tools: int = 0
    agents: int = 0


class ToolOut(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    dangerous: bool = False


# ---------------------------------------------------------------------------
# Import / Export (0.1.2 #7)
# ---------------------------------------------------------------------------
class ExportRequest(BaseModel):
    """Request body for the bulk export endpoint (POST /export).

    Using a JSON body rather than query parameters avoids the
    ``?agent_ids=a&agent_ids=b`` vs ``?agent_ids=a`` ambiguity for
    single-id exports.
    """

    agent_ids: list[str] | None = Field(
        default=None,
        description=(
            "Specific agent IDs to export.  Omit (or pass null) to "
            "export every agent in the database."
        ),
    )


__all__ = [
    "AgentCreate",
    "AgentUpdate",
    "AgentOut",
    "RunCreate",
    "RunOut",
    "RunSummary",
    "StepOut",
    "EvalCaseIn",
    "EvalCreate",
    "EvalCaseOut",
    "EvalRunOut",
    "EvalUploadResponse",
    "MemoryIn",
    "MemoryOut",
    "HealthOut",
    "ToolOut",
    "ExportRequest",
]
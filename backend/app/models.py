"""
SQLAlchemy ORM models for AgentForge.

These tables are consumed by the API layer (``app/api``) and the agent loop
(``app/agent/loop``).  The schema is intentionally rich (Agent + Memory +
Run + Step) and stores JSON payloads as TEXT/JSON columns so it works on
both SQLite (tests / dev) and PostgreSQL (prod).

Tables
------
- Agent        : persistent definition of an agent
- Memory       : long-term memory entries attached to an Agent
- Run          : a single execution of an Agent against a user task
- Step         : one step in a Run's execution trace
- EvalCase     : a single evaluation case
- EvalRun      : one evaluation run over a set of EvalCase ids
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

import json as _json

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship
from sqlalchemy.types import JSON

# Use the shared declarative base from ``app.database`` so that
# ``Base.metadata`` is the same instance everywhere (tests rely on this).
from app.database import Base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Naive UTC now; works the same on SQLite and Postgres."""
    return datetime.utcnow()


def _new_uuid() -> str:
    """Generate a new UUID4 string.  Used as ``default`` for every
    primary key so callers don't have to plumb an id through."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Users and ownership
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_team_org_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_membership_team_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    team_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="developer")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class AuditLog(Base):
    """Append-only security and resource audit event."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    actor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, index=True)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    team_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requester_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    resolver_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    resolution_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent(Base):
    """A persistent definition of an agent."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )

    owner_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    team_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    model: Mapped[str] = mapped_column(
        String(100), nullable=False, default="gpt-4o-mini"
    )
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="openai"
    )
    temperature: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.7
    )
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=20)

    # JSON-serialised list of tool names + free-form key/value config.
    tools: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    # Relationships
    runs: Mapped[list["Run"]] = relationship(
        "Run",
        back_populates="agent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    memories: Mapped[list["Memory"]] = relationship(
        "Memory",
        back_populates="agent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    eval_runs: Mapped[list["EvalRun"]] = relationship(
        "EvalRun",
        back_populates="agent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Agent id={self.id!r} name={self.name!r} model={self.model!r}>"


# ---------------------------------------------------------------------------
# Prompt templates and immutable versions
# ---------------------------------------------------------------------------


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"
    __table_args__ = (UniqueConstraint("agent_id", "name", name="uq_prompt_template_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("template_id", "version", name="uq_prompt_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("prompt_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


class Memory(Base):
    """Long-term memory entry attached to an Agent."""

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(50), nullable=False, default="episodic"
    )
    importance: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5
    )
    meta: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Vector-memory additions (0.1.2).  See ``app/memory/embeddings.py`` and
    # ``app/memory/vector_store.py`` for the codec and the in-process store.
    # ``embedding`` stores a packed float32 little-endian blob; NULL means
    # the entry predates the migration or was never embedded.
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # ``session_id`` scopes memory to a single Run; the ``MemoryStore.add``
    # constructor already accepts it on 0.1.0 so the column has to exist
    # for that kwarg to work.
    session_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    # TTL column (0.1.2 4.3).  ``NULL`` means "never expires".  ``index=True``
    # so the periodic cleanup query (and the read-side filter in
    # ``MemoryStore.list`` / ``MemoryStore.search``) is O(log n) instead of
    # a full table scan.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )

    agent: Mapped["Agent"] = relationship("Agent", back_populates="memories")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Memory id={self.id!r} agent_id={self.agent_id!r} kind={self.kind!r}>"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


# Run status constants
RUN_STATUS_PENDING = "pending"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCESS = "success"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_PAUSED = "paused"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_OK = "ok"
RUN_STATUS_ERROR = "error"
RUN_STATUSES: tuple[str, ...] = (
    RUN_STATUS_PENDING,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCESS,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_OK,
    RUN_STATUS_ERROR,
)


class Run(Base):
    """A single execution of an Agent against a user task."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    agent_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("agent_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    input: Mapped[str] = mapped_column(Text, nullable=False)
    output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RUN_STATUS_PENDING, index=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Immutable per-run execution snapshot.  Request-level overrides must
    # never be written back to the Agent definition.
    config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Pause / resume snapshot (JSON serialised RunState).  When the
    # loop pauses this column holds the full context + counters; on
    # resume() it is loaded back and the loop continues.
    state: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default="")

    # Optional grouping key (e.g. a conversation/session id).
    session_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )

    agent: Mapped["Agent"] = relationship("Agent", back_populates="runs")
    steps: Mapped[list["Step"]] = relationship(
        "Step",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Step.index",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Run id={self.id!r} agent_id={self.agent_id!r} status={self.status!r}>"


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------


# Step kind constants
STEP_KIND_THINK = "think"
STEP_KIND_TOOL_CALL = "tool_call"
STEP_KIND_TOOL_RESULT = "tool_result"
STEP_KIND_FINAL_ANSWER = "final_answer"
STEP_KIND_OBSERVATION = "observation"
STEP_KIND_ERROR = "error"
STEP_KINDS: tuple[str, ...] = (
    STEP_KIND_THINK,
    STEP_KIND_TOOL_CALL,
    STEP_KIND_TOOL_RESULT,
    STEP_KIND_FINAL_ANSWER,
    STEP_KIND_OBSERVATION,
    STEP_KIND_ERROR,
)


# Step status constants
STEP_STATUS_OK = "ok"
STEP_STATUS_SUCCESS = "success"
STEP_STATUS_ERROR = "error"
STEP_STATUS_RUNNING = "running"
STEP_STATUSES: tuple[str, ...] = (
    STEP_STATUS_OK,
    STEP_STATUS_SUCCESS,
    STEP_STATUS_ERROR,
    STEP_STATUS_RUNNING,
)


class Step(Base):
    """One step in a Run's execution trace."""

    __tablename__ = "agent_steps"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STEP_STATUS_OK
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )

    run: Mapped["Run"] = relationship("Run", back_populates="steps")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Step id={self.id!r} run_id={self.run_id!r} "
            f"index={self.index} kind={self.kind!r}>"
        )


# ---------------------------------------------------------------------------
# Eval
# ---------------------------------------------------------------------------


EVAL_STATUS_PENDING = "pending"
EVAL_STATUS_RUNNING = "running"
EVAL_STATUS_COMPLETED = "completed"
EVAL_STATUS_FAILED = "failed"
EVAL_STATUS_CANCELLING = "cancelling"
EVAL_STATUS_CANCELLED = "cancelled"
EVAL_STATUSES: tuple[str, ...] = (
    EVAL_STATUS_PENDING,
    EVAL_STATUS_RUNNING,
    EVAL_STATUS_COMPLETED,
    EVAL_STATUS_FAILED,
    EVAL_STATUS_CANCELLING,
    EVAL_STATUS_CANCELLED,
)


class EvalCase(Base):
    """A single evaluation case (input + optional expectations + result).

    Originally the table only held the *input* side of a case.  To
    surface per-case scoring in the UI (and to avoid a parallel JSON
    blob for every result row) the result fields live on the same row,
    filled in by :class:`app.eval.runner.EvalRunner` when the case is
    executed.
    """

    __tablename__ = "eval_cases"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    agent_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)

    # Optional FK to the EvalRun that *owns* this row.  When the case
    # is a "template" (still in the dataset) ``eval_run_id`` is NULL.
    eval_run_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("eval_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Free-form dataset name so callers can group cases.
    dataset: Mapped[str] = mapped_column(
        String(100), nullable=False, default="default", index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    input: Mapped[str] = mapped_column(Text, nullable=False)
    expected: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # ---- Per-case result fields (filled in after a run) -------------
    index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    passed: Mapped[bool] = mapped_column(
        # SQLAlchemy doesn't allow ``bool`` as a column type; use Integer
        # underneath and translate in/out.
        Integer, nullable=False, default=0
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # JSON-serialised list of tool call dicts for the UI.
    tool_calls: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    # JSON-serialised EvalScore.details for the UI.
    score_details: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EvalCase id={self.id!r} name={self.name!r} dataset={self.dataset!r}>"


# ---------------------------------------------------------------------------
# Tool metadata (0.1.3 plugin layer; resolves the "tools are hard-coded
# in tools.py" placeholder in the 0.1.0 roadmap).
#
# Each row is a runtime override of the static :class:`ToolSpec` shipped
# in ``app.agent.tools``.  The agent loop and the ``GET /api/tools``
# endpoint both read from this table first; if a tool has no row here,
# the source-of-truth from the registry is used.
#
# This makes the metadata mutable without a process restart (the main
# 0.1.0 limitation) and gives the front-end a place to add richer
# metadata that the registry does not know about (category, display
# name, enabled/disabled flag).
# ---------------------------------------------------------------------------


class ToolMetadata(Base):
    """Runtime metadata override for a tool.

    Rows are sparse -- most columns default to ``None`` so the
    read-side merge can decide which fields are actually overridden.
    A row is created lazily the first time the front-end edits a
    tool's metadata; until then the registry's defaults win.
    """

    __tablename__ = "tool_metadata"

    # Tool name is the natural primary key.  We do not FK to
    # ``tools.py`` because tools are a Python registry, not a table.
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)

    # Optional display name (frontend-only).
    display_name: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )

    # Overrides of the source-of-truth spec.  ``None`` means "use the
    # value from the registry".  The agent loop / ``GET /api/tools``
    # merge these on read.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    risk: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    scope: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # JSON-serialised parameters schema (overrides ``ToolSpec.schema``).
    parameters_schema: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # When ``False`` the tool is hidden from the ``/api/tools`` listing
    # and is excluded from the agent's prompt; a tool that is
    # registered but disabled cannot be called.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return the metadata as a JSON-safe dict.

        ``parameters_schema`` is round-tripped as ``dict`` so the
        front-end never has to know whether the column was a string
        or a structured object.
        """
        params = self.parameters_schema
        if params:
            try:
                params_obj = _json.loads(params)
            except (TypeError, ValueError):
                params_obj = None
        else:
            params_obj = None
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "risk": self.risk,
            "scope": self.scope,
            "category": self.category,
            "parameters_schema": params_obj,
            "enabled": bool(self.enabled),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ToolMetadata name={self.name!r} risk={self.risk!r} "
            f"enabled={self.enabled!r}>"
        )


# ---------------------------------------------------------------------------
# EvalRun
# ---------------------------------------------------------------------------


class EvalRun(Base):
    """One evaluation run: runs an Agent against a set of EvalCase ids."""

    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)

    dataset: Mapped[str] = mapped_column(
        String(100), nullable=False, default="default"
    )
    scorer: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    pass_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.7
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Immutable request snapshot used by persistent workers and retries.
    request: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # JSON list of case results
    results: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=EVAL_STATUS_PENDING,
        index=True,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    agent: Mapped["Agent"] = relationship("Agent", back_populates="eval_runs")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<EvalRun id={self.id!r} agent_id={self.agent_id!r} "
            f"status={self.status!r} passed={self.passed}/{self.total}>"
        )


# ---------------------------------------------------------------------------
# Agent versions and quality gates
# ---------------------------------------------------------------------------


class AgentVersion(Base):
    """Immutable runtime configuration captured whenever an Agent is saved."""

    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version_number", name="uq_agent_version_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    tools: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    gate_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    created_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )


class EvalSuite(Base):
    """A named, fixed regression set and its pass-rate threshold."""

    __tablename__ = "eval_suites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    agent_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    case_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    pass_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.95)
    scorer: Mapped[str] = mapped_column(String(100), nullable=False, default="composite")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_on_save: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    schedule_interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class GatePolicy(Base):
    """Singleton quality-gate policy; suite thresholds override the default."""

    __tablename__ = "gate_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    default_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.95)
    enforce: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_bypass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_to: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class GateRun(Base):
    """Persisted quality decision for one AgentVersion and EvalSuite."""

    __tablename__ = "gate_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    agent_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    suite_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("eval_suites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    eval_run_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("eval_runs.id", ondelete="SET NULL"), nullable=True
    )
    baseline_gate_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("gate_runs.id", ondelete="SET NULL"), nullable=True
    )
    triggered_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    trigger_source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    pass_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.95)
    failed_case_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    details: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    diff: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now(), index=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class GateBypass(Base):
    """Append-only, reasoned administrative exception to a failed gate."""

    __tablename__ = "gate_bypasses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    agent_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gate_run_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("gate_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.now()
    )


class ModelPrice(Base):
    """Append-only provider/model token price effective from a UTC timestamp."""

    __tablename__ = "model_prices"
    __table_args__ = (
        UniqueConstraint("provider", "model", "effective_from", name="uq_model_price_effective"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_per_million: Mapped[float] = mapped_column(Float, nullable=False)
    output_per_million: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    context_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    eval_case_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    suite_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    rule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, index=True)
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    agent_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    runtime_spec: Mapped[str] = mapped_column(Text, nullable=False)
    version_history: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    published_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class AgentTemplate(Base):
    __tablename__ = "agent_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="general")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class AgentTemplateVersion(Base):
    __tablename__ = "agent_template_versions"
    __table_args__ = (UniqueConstraint("template_id", "version_number", name="uq_agent_template_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    team_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kb_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class KnowledgeBinding(Base):
    __tablename__ = "knowledge_bindings"
    __table_args__ = (UniqueConstraint("agent_id", "kb_id", name="uq_agent_knowledge_binding"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


# ---------------------------------------------------------------------------
# JSON-Text auto-serialisation
# ---------------------------------------------------------------------------
#
# Several ``Text`` columns above actually store JSON (lists / dicts).  Some
# call-sites — notably the FastAPI handlers and the eval runner — pass
# Python ``list``/``dict`` values directly to the ORM.  We attach
# ``before_insert`` / ``before_update`` mapper-level listeners so that
# those values are transparently ``json.dumps``-ed before they reach
# the driver.  This is the single source of truth — every code path
# now works regardless of whether it pre-serialised the value or not.
#
# Mapper-level events (rather than ``Session.before_flush``) are used
# because they fire for *every* ORM mutation regardless of whether the
# session is sync, async, or being driven from inside a greenlet.

# Tuple of ``(model class, attribute name)`` pairs that store JSON in
# Text columns.  Keep this list in sync with the column declarations
# above.
_JSON_TEXT_FIELDS: tuple[tuple[type, str], ...] = (
    (Agent, "tools"),
    (Agent, "config"),
    (Memory, "meta"),
    (Step, "payload"),
    (EvalCase, "meta"),
    (EvalCase, "tool_calls"),
    (EvalCase, "score_details"),
    (EvalRun, "results"),
    (EvalRun, "request"),
    (AgentVersion, "snapshot"),
    (AgentVersion, "tools"),
    (AgentVersion, "config"),
    (EvalSuite, "case_ids"),
    (GatePolicy, "notify_to"),
    (GateRun, "failed_case_ids"),
    (GateRun, "details"),
    (GateRun, "diff"),
    (AuditLog, "detail"),
    (ApprovalRequest, "payload"),
    (Feedback, "context_snapshot"),
    (AlertEvent, "detail"),
    (Deployment, "runtime_spec"),
    (Deployment, "version_history"),
    (AgentTemplateVersion, "snapshot"),
)


# Default JSON payload (as a string) for JSON-Text columns that are
# declared ``nullable=False``.  Used by ``_jsonify`` below so that
# callers can safely assign ``None`` to "should-be-JSON" attributes
# (e.g. during partial updates) without tripping the NOT NULL
# constraint.
_JSON_TEXT_NOTNULL_DEFAULTS: dict[tuple[type, str], str] = {
    (EvalRun, "results"): "[]",
}


def _jsonify(*args) -> None:  # type: ignore[no-untyped-def]
    """Encode any list/dict attributes that map to JSON-Text columns.

    Mapper-level ``before_insert`` / ``before_update`` listeners are
    invoked as ``fn(mapper, connection, target)``.  We accept *args and
    pick out the target instance so the same hook works regardless of
    whether the ORM calls it with one positional argument (older
    SQLAlchemy) or three (current SQLAlchemy 2.x).
    """
    target = args[-1]
    for model_cls, attr in _JSON_TEXT_FIELDS:
        if not isinstance(target, model_cls):
            continue
        current = getattr(target, attr, None)
        if current is None:
            # The column is ``nullable=True`` for most JSON-Text
            # fields (e.g. ``Run.state``) so a ``None`` there is
            # allowed.  For NOT NULL JSON-Text columns, fall back to
            # a sensible default JSON payload so partial updates
            # (e.g. the eval runner resetting ``results`` to
            # ``None`` before re-running) don't trip the database.
            default = _JSON_TEXT_NOTNULL_DEFAULTS.get(
                (model_cls, attr)
            )
            if default is not None:
                setattr(target, attr, default)
            continue
        if not isinstance(current, (str, bytes)):
            setattr(target, attr, _json.dumps(current, ensure_ascii=False))


# Attach at the mapper level so that both sync and async sessions
# see the hook.  SQLAlchemy 2.x: ``MapperEvents.before_insert`` /
# ``before_update`` are per-mapper; we register them for every
# involved class once.
from sqlalchemy.orm import Mapper  # noqa: E402
import sqlalchemy.orm  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402  (re-import kept for backwards compat)


for _model_cls, _attr in _JSON_TEXT_FIELDS:
    # ``listen`` at the class level is a shortcut for ``listen(SomeClass, ...)``.
    event.listen(_model_cls, "before_insert", _jsonify)
    event.listen(_model_cls, "before_update", _jsonify)


@event.listens_for(AgentVersion, "before_update")
def _protect_agent_version(_mapper: Mapper, _connection: Any, target: AgentVersion) -> None:
    """Only the mutable gate decision may change after a snapshot is committed."""
    state = sqlalchemy.inspect(target)
    protected = (
        "agent_id", "team_id", "version_number", "snapshot", "system_prompt", "model",
        "provider", "temperature", "max_steps", "tools", "config", "created_by", "created_at",
    )
    if any(state.attrs[name].history.has_changes() for name in protected):
        raise ValueError("AgentVersion runtime snapshots are immutable")


@event.listens_for(AgentVersion, "before_delete")
def _prevent_agent_version_delete(_mapper: Mapper, _connection: Any, _target: AgentVersion) -> None:
    raise ValueError("AgentVersion records cannot be deleted through the ORM")


@event.listens_for(GateBypass, "before_update")
@event.listens_for(GateBypass, "before_delete")
def _protect_gate_bypass(_mapper: Mapper, _connection: Any, _target: GateBypass) -> None:
    raise ValueError("GateBypass audit records are append-only")


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _protect_audit_log(_mapper: Mapper, _connection: Any, _target: AuditLog) -> None:
    raise ValueError("AuditLog records are append-only")


__all__ = [
    "Base",
    # Constants
    "RUN_STATUSES",
    "STEP_KINDS",
    "STEP_STATUSES",
    "EVAL_STATUSES",
    "RUN_STATUS_PENDING",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_SUCCESS",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_CANCELLED",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_OK",
    "RUN_STATUS_ERROR",
    "RUN_STATUS_PAUSED",
    "STEP_KIND_THINK",
    "STEP_KIND_TOOL_CALL",
    "STEP_KIND_TOOL_RESULT",
    "STEP_KIND_FINAL_ANSWER",
    "STEP_KIND_OBSERVATION",
    "STEP_KIND_ERROR",
    "STEP_STATUS_OK",
    "STEP_STATUS_SUCCESS",
    "STEP_STATUS_ERROR",
    "STEP_STATUS_RUNNING",
    "EVAL_STATUS_PENDING",
    "EVAL_STATUS_RUNNING",
    "EVAL_STATUS_COMPLETED",
    "EVAL_STATUS_FAILED",
    # Models
    "Agent",
    "Organization",
    "Team",
    "Membership",
    "AuditLog",
    "ApprovalRequest",
    "ModelPrice",
    "Feedback",
    "AlertRule",
    "AlertEvent",
    "Deployment",
    "AgentTemplate",
    "AgentTemplateVersion",
    "KnowledgeBase",
    "KnowledgeDocument",
    "KnowledgeChunk",
    "KnowledgeBinding",
    "Memory",
    "Run",
    "Step",
    "EvalCase",
    "EvalRun",
    "AgentVersion",
    "EvalSuite",
    "GatePolicy",
    "GateRun",
    "GateBypass",
    "ToolMetadata",
]
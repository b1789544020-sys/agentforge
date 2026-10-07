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
# Agent
# ---------------------------------------------------------------------------


class Agent(Base):
    """A persistent definition of an agent."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid
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

    input: Mapped[str] = mapped_column(Text, nullable=False)
    output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RUN_STATUS_PENDING, index=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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
EVAL_STATUSES: tuple[str, ...] = (
    EVAL_STATUS_PENDING,
    EVAL_STATUS_RUNNING,
    EVAL_STATUS_COMPLETED,
    EVAL_STATUS_FAILED,
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

    dataset: Mapped[str] = mapped_column(
        String(100), nullable=False, default="default"
    )
    scorer: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    pass_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.7
    )

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
    "Memory",
    "Run",
    "Step",
    "EvalCase",
    "EvalRun",
    "ToolMetadata",
]
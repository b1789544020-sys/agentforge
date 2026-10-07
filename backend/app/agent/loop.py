"""Agent Loop — the core orchestration engine of AgentForge.

This module implements two flavours of the agent loop:

1. :class:`AgentLoop`  — the *production* loop.  It is bound to a
   SQLAlchemy :class:`Agent` row and an :class:`AsyncSession`, persists
   every step into the ``agent_steps`` table, supports cancellation,
   pause / resume via a persisted state column, and exposes a
   :class:`LoopResult` that the API layer turns into JSON.

2. :class:`AgentLoopV2` — the *clean-room* implementation that exactly
   matches the dataclass-style contract requested in the design spec::

       AgentLoop(config: AgentConfig, llm_client: LLMClient,
                 tool_executor: ToolExecutor, context_manager: ContextManager)

   It returns an :class:`AgentRunResult` and is independent of the
   database (so it's easy to unit-test in isolation).  The two classes
   share the same per-step algorithm via the internal
   :func:`_step_once` helper.

Edge cases handled
------------------

* Token budget overrun       -> :meth:`Context.enforce_budget`
* Step limit overrun         -> status ``terminated`` / final answer
* Tool call failure          -> error string fed back to the model
* 3 consecutive tool errors  -> status ``failed`` (per spec)
* LLM timeout                -> caught, classified, terminated
* Pause / resume             -> state column on ``Run`` table
* Cancellation (cancel())    -> status ``cancelled``
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import Context
from app.agent.tools import (
    ToolError,
    ToolExecutor,
    ToolRegistry,
    registry as default_registry,
)
from app.config import get_settings
from app.llm.client import LLMClient, LLMResponse, MockLLMClient
from app.models import (
    Agent,
    Run,
    Step,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCESS,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_PAUSED,
    STEP_STATUS_OK,
    STEP_STATUS_ERROR,
    STEP_KIND_THINK,
    STEP_KIND_TOOL_CALL,
    STEP_KIND_TOOL_RESULT,
    STEP_KIND_FINAL_ANSWER,
    STEP_KIND_OBSERVATION,
    STEP_KIND_ERROR,
)

logger = logging.getLogger(__name__)


# 0.1.2 5.1 轮: SQLite is a single-writer database.  When the eval
# runner fans out cases concurrently, multiple ``AgentLoop`` instances
# share the same engine and may collide on the write lock.  These
# tiny retry helpers transparently serialise a brief contention
# window so concurrent cases don't surface ``database is locked``
# to the caller.
async def _commit_with_retry(session: AsyncSession, retries: int = 5) -> None:
    """Commit with retry on SQLite's ``database is locked`` transient errors.

    Catches both ``OperationalError`` and the ``PendingRollbackError``
    SQLAlchemy raises on the *next* commit after a failed one.  On
    retry we ``rollback()`` to clear the session state and re-issue
    the commit; the ORM objects remain attached so the UoW is
    preserved.
    """
    for attempt in range(retries):
        try:
            await session.commit()
            return
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == retries - 1:
                raise
            try:
                await session.rollback()
            except Exception:
                raise
            await asyncio.sleep(0.05 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 - PendingRollbackError etc.
            if "rolled back" not in str(exc).lower() or attempt == retries - 1:
                raise
            try:
                await session.rollback()
            except Exception:
                raise
            await asyncio.sleep(0.05 * (attempt + 1))


async def _flush_with_retry(session: AsyncSession, retries: int = 5) -> None:
    for attempt in range(retries):
        try:
            await session.flush()
            return
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == retries - 1:
                raise
            try:
                await session.rollback()
            except Exception:
                raise
            await asyncio.sleep(0.05 * (attempt + 1))


# ---------------------------------------------------------------------------
# Constants — run states
# ---------------------------------------------------------------------------

# Run lifecycle
STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_TERMINATED = "terminated"  # hit step/token limit, not an error

# Map a state -> the matching Run.status column value
_STATE_TO_RUN_STATUS: dict[str, str] = {
    STATE_PENDING: "pending",
    STATE_RUNNING: RUN_STATUS_RUNNING,
    STATE_PAUSED: RUN_STATUS_PAUSED,
    STATE_COMPLETED: RUN_STATUS_SUCCESS,
    STATE_FAILED: RUN_STATUS_FAILED,
    STATE_CANCELLED: RUN_STATUS_CANCELLED,
    STATE_TERMINATED: "terminated",
}

# Per-spec: 3 consecutive tool errors -> terminate
MAX_CONSECUTIVE_TOOL_ERRORS = 3


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AgentCancelledError(RuntimeError):
    """Raised when the loop is cancelled via :meth:`AgentLoop.cancel`."""


class AgentPausedError(RuntimeError):
    """Raised internally when the loop hits a pause checkpoint.

    Caught by :meth:`AgentLoop.run` which then persists the state
    before returning.
    """


class AgentTerminated(RuntimeError):
    """Internal signal that a hard limit (steps / tokens) was reached."""


# ---------------------------------------------------------------------------
# Result / config dataclasses (production contract)
# ---------------------------------------------------------------------------


@dataclass
class LoopResult:
    """The final result of a single :class:`AgentLoop` execution."""

    run_id: str
    output: str
    status: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    step_count: int = 0
    error: Optional[str] = None


@dataclass
class LoopConfig:
    """Runtime overrides for :class:`AgentLoop`."""

    max_steps: Optional[int] = None
    temperature: Optional[float] = None
    model: Optional[str] = None
    on_step: Optional[Callable[[Step], None]] = None
    # Pause / resume hooks
    on_pause: Optional[Callable[["RunState"], None]] = None
    on_resume: Optional[Callable[["RunState"], None]] = None
    max_consecutive_errors: int = MAX_CONSECUTIVE_TOOL_ERRORS


# ---------------------------------------------------------------------------
# Spec-style dataclasses (used by AgentLoopV2 + AgentRunResult)
# ---------------------------------------------------------------------------


@dataclass
class TraceStep:
    """A single, framework-agnostic step in the agent trace.

    Returned inside :class:`AgentRunResult.steps`.  It mirrors the
    columns of the ``agent_steps`` DB table but is decoupled from it.
    """

    index: Optional[int] = None
    kind: str = ""            # think | tool_call | tool_result | final_answer | observation | error
    name: Optional[str] = None
    content: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    tokens: int = 0
    status: str = STEP_STATUS_OK
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    # Free-form bag for extra information callers want to attach to a step
    # (e.g. retry attempts, request-id, provider metadata).  Stored on the
    # JSON ``metadata`` column of ``agent_steps`` so the test suite can
    # assert on it.
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "name": self.name,
            "content": self.content,
            "payload": self.payload,
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "status": self.status,
            "error": self.error,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "metadata": dict(self.metadata),
        }


@dataclass
class AgentRunResult:
    """Public return value of :class:`AgentLoopV2.run`."""

    final_answer: str
    steps: list[TraceStep]
    total_steps: int
    total_tokens: int
    total_latency_ms: int
    status: str  # completed | failed | terminated
    error_message: Optional[str] = None


@dataclass
class AgentConfig:
    """Spec-style agent configuration object.

    Distinct from the SQLAlchemy :class:`Agent` ORM model.  The
    production loop builds one of these from an ORM row via
    :meth:`AgentConfig.from_orm`.
    """

    agent_id: str
    name: str
    model: str
    system_prompt: str = ""
    tools: list[str] = field(default_factory=list)
    max_steps: int = 20
    temperature: float = 0.7
    max_context_tokens: int = 8000
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_orm(cls, agent: Agent) -> "AgentConfig":
        return cls(
            agent_id=agent.id,
            name=agent.name,
            model=agent.model or "gpt-4o-mini",
            system_prompt=agent.system_prompt or "",
            tools=_safe_json_loads(agent.tools, []),
            max_steps=int(agent.max_steps or 20),
            temperature=float(agent.temperature if agent.temperature is not None else 0.7),
            max_context_tokens=int(
                _safe_json_loads(agent.config, {}).get("max_context_tokens", 8000)
            ),
            extra=_safe_json_loads(agent.config, {}),
        )


@dataclass
class RunState:
    """Snapshot of a paused / running agent loop.

    Persisted to the ``Run.state`` JSON column whenever the loop
    pauses, fails, or completes.  Loading it back and passing it to
    :meth:`AgentLoop.resume` continues from the last completed step.
    """

    run_id: str
    task: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    step_index: int = 0
    consecutive_errors: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    last_status: str = STATE_RUNNING
    final_answer: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "run_id": self.run_id,
                "task": self.task,
                "messages": self.messages,
                "step_index": self.step_index,
                "consecutive_errors": self.consecutive_errors,
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_completion_tokens": self.total_completion_tokens,
                "last_status": self.last_status,
                "final_answer": self.final_answer,
                "extra": self.extra,
            },
            ensure_ascii=False,
            default=str,
        )

    @classmethod
    def from_json(cls, raw: Any) -> "RunState":
        if not raw:
            return cls(run_id="", task="")
        if isinstance(raw, dict):
            data = raw
        else:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                return cls(run_id="", task="")
        return cls(
            run_id=data.get("run_id", "") or "",
            task=data.get("task", "") or "",
            messages=list(data.get("messages", []) or []),
            step_index=int(data.get("step_index", 0) or 0),
            consecutive_errors=int(data.get("consecutive_errors", 0) or 0),
            total_prompt_tokens=int(data.get("total_prompt_tokens", 0) or 0),
            total_completion_tokens=int(
                data.get("total_completion_tokens", 0) or 0
            ),
            last_status=data.get("last_status", STATE_RUNNING) or STATE_RUNNING,
            final_answer=data.get("final_answer", "") or "",
            extra=dict(data.get("extra", {}) or {}),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


def _step_to_dict(step: Step) -> dict[str, Any]:
    payload = _safe_json_loads(step.payload, {})
    # 0.1.3 #9 — risk badge.  Mirror the field in ``api.runs._serialize_step``
    # so the WebSocket ``run.step`` events expose the same shape the
    # REST ``GET /runs/{id}`` endpoint does.  See the agent-loop
    # tool_call recording for where ``payload["risk"]`` is written.
    risk_value: str | None = None
    if step.kind == "tool_call" and isinstance(payload, dict):
        v = payload.get("risk")
        if isinstance(v, str) and v:
            risk_value = v
    return {
        "id": step.id,
        "index": step.index,
        "kind": step.kind,
        "name": step.name,
        "content": step.content,
        "payload": payload,
        "duration_ms": step.duration_ms,
        "tokens": step.tokens,
        "status": step.status,
        "error": step.error,
        "created_at": step.created_at.isoformat() if step.created_at else None,
        "risk": risk_value,
    }


def _classify_exception(exc: BaseException) -> tuple[str, bool]:
    """Return ``(label, retryable)`` for an exception."""
    from app.llm.client import (
        LLMError,
        LLMAuthError,
        LLMRateLimitError,
        LLMResponseError,
        LLMTimeoutError,
    )

    if isinstance(exc, LLMTimeoutError):
        return "llm_timeout", False
    if isinstance(exc, LLMRateLimitError):
        return "llm_rate_limit", True
    if isinstance(exc, LLMAuthError):
        return "llm_auth", False
    if isinstance(exc, LLMResponseError):
        return "llm_response", False
    if isinstance(exc, LLMError):
        return "llm", True
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout", False
    if isinstance(exc, ToolError):
        return "tool", True
    if isinstance(exc, AgentCancelledError):
        return "cancelled", False
    return exc.__class__.__name__, False


# ---------------------------------------------------------------------------
# Core step algorithm — shared by AgentLoop and AgentLoopV2
# ---------------------------------------------------------------------------


async def _step_once(
    *,
    context: Context,
    llm: LLMClient,
    tool_exec: ToolExecutor,
    tool_registry: ToolRegistry,
    enabled_tools: Optional[list[str]],
    model: str,
    temperature: float,
    max_context_tokens: int,
    db_session: Optional[AsyncSession] = None,
    agent_id: Optional[str] = None,
) -> dict[str, Any]:
    """Run *one* LLM call + (optionally) tool execution.

    Returns a dict with::

        {"response": LLMResponse,
         "tool_results": [(call, result_str, error), ...],
         "enforced_budget": bool,
         "duration_ms": int}
    """
    # (b) check + enforce token budget
    enforced = False
    if context.total_tokens() > max_context_tokens:
        context.enforce_budget()
        enforced = True

    # (a) construct messages + (d) call LLM
    # 0.1.3 #3 -- when a DB session is available, intersect the
    # agent's enabled-tools whitelist with the per-tool ``enabled``
    # override from the ``tool_metadata`` table.  This lets the
    # front-end disable a tool globally (e.g. ``shell_exec``) without
    # having to re-save every agent that lists it.  A missing table
    # row means "use the registry default" (i.e. enabled), which keeps
    # pre-0.1.3 installs working without a migration.
    effective_tools: Optional[list[str]] = enabled_tools
    if db_session is not None and enabled_tools:
        try:
            from app.api.tools_metadata import get_enabled_tool_names

            allowed = await get_enabled_tool_names(
                db_session, list(enabled_tools)
            )
            effective_tools = [t for t in enabled_tools if t in allowed]
        except Exception:  # noqa: BLE001 - never let metadata fail the run
            effective_tools = enabled_tools
    tool_schemas = tool_registry.to_openai_tools(effective_tools)
    t0 = time.perf_counter()
    response: LLMResponse = await llm.chat(
        context.to_openai(),
        tools=tool_schemas or None,
        model=model,
        temperature=temperature,
    )
    llm_ms = int((time.perf_counter() - t0) * 1000)

    # Record the assistant message in the context (always — even on
    # final answers, so the trace is reproducible).
    assistant_msg: dict[str, Any] = {
        "role": "assistant",
        "content": response.content or "",
    }
    if response.tool_calls:
        assistant_msg["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.name,
                    "arguments": json.dumps(c.arguments, ensure_ascii=False),
                },
            }
            for c in response.tool_calls
        ]
    context.messages.append(assistant_msg)

    # Execute tools (if any)
    tool_results: list[tuple[Any, str, Optional[str]]] = []
    for call in response.tool_calls:
        t1 = time.perf_counter()
        try:
            tool_ctx: Dict[str, Any] = {}
            if db_session is not None:
                tool_ctx["session"] = db_session
            if agent_id is not None:
                tool_ctx["agent_id"] = agent_id
            result = await tool_exec.execute(
                call.name, call.arguments, ctx=tool_ctx or None
            )
            observation = _stringify(result)
            tool_results.append((call, observation, None))
        except ToolError as exc:
            # (f.1) feed the error back to the model so it can recover
            err_msg = f"Error: {exc}"
            tool_results.append((call, err_msg, str(exc)))
        except Exception as exc:  # noqa: BLE001
            err_msg = f"Error: {exc.__class__.__name__}: {exc}"
            tool_results.append((call, err_msg, str(exc)))
        finally:
            tool_ms = int((time.perf_counter() - t1) * 1000)
            tool_results[-1] = (*tool_results[-1][:-1], tool_results[-1][-1])

        # Add the tool result to the context so the model can see it
        # next iteration.  When the call_id is missing, fall back to a
        # synthetic id to keep providers happy.
        last_call, last_observation, _ = tool_results[-1]
        context.add_tool_result(last_call.id or f"call_{uuid.uuid4().hex[:8]}", last_observation)

    return {
        "response": response,
        "tool_results": tool_results,
        "enforced_budget": enforced,
        "duration_ms": llm_ms,
    }


# ---------------------------------------------------------------------------
# Production AgentLoop — bound to ORM, supports cancel + pause/resume
# ---------------------------------------------------------------------------


class AgentLoop:
    """Drive the think-act-observe cycle for a single :class:`Agent`.

    The constructor signature is preserved (agent / session / llm /
    tools / config) so the API layer keeps working, but the
    implementation is rewritten against the new spec:

    * :meth:`run` returns a :class:`LoopResult` *or* raises
      :class:`AgentPausedError` (which :meth:`run` catches and turns
      into a paused :class:`LoopResult`).
    * :meth:`pause` flips an internal flag and the loop writes its
      state into the ``Run.state`` JSON column at the next checkpoint.
    * :meth:`resume` rebuilds the loop from the persisted state.
    """

    def __init__(
        self,
        agent: Agent,
        session: AsyncSession,
        *,
        llm: Optional[LLMClient] = None,
        tools: Optional[ToolRegistry] = None,
        tool_executor: Optional[ToolExecutor] = None,
        context_manager: Optional[Context] = None,
        config: Optional[LoopConfig] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.agent = agent
        self.session = session
        self.session_id = session_id
        self.settings = get_settings()
        self.llm = llm or self._build_default_llm()

        # Tool wiring: the registry is what we ask for OpenAI schemas;
        # the executor is what we actually call.  When callers only
        # pass a registry, wrap it in a default ToolExecutor.
        self.tools = tools or default_registry
        self.tool_executor = tool_executor or ToolExecutor(self.tools)
        self.config = config or LoopConfig()

        # Optional pre-built context manager (a :class:`Context`).
        # Tests / AgentLoopV2 may pass one in; otherwise we build
        # one at run() time.
        self._injected_context = context_manager

        # Concurrency primitives
        self._cancelled = asyncio.Event()
        self._pause = asyncio.Event()
        self._run: Optional[Run] = None

        # Pre-parse JSON columns on the agent for fast access.
        self._agent_tools: list[str] = _safe_json_loads(agent.tools, [])
        self._agent_config: dict[str, Any] = _safe_json_loads(agent.config, {})

    # ---- external signals --------------------------------------------

    def cancel(self) -> None:
        """Cooperatively cancel a running loop."""
        self._cancelled.set()

    def pause(self) -> None:
        """Cooperatively pause a running loop.

        The next checkpoint will flush the current state to the
        ``Run.state`` column and return a paused :class:`LoopResult`.
        """
        self._pause.set()

    def resume_signal(self) -> None:
        """Clear the pause flag (resume from the API side)."""
        self._pause.clear()

    # ---- internal helpers -------------------------------------------

    def _build_default_llm(self) -> LLMClient:
        """Construct the LLM client for this run.

        Resolution order:

        1. If ``AGENTFORGE_LLM_PROVIDER=mock`` (env) or the agent's
           ``provider == "mock"`` → return :class:`MockLLMClient`.
        2. If no real key is configured for any non-mock provider → fall
           back to :class:`MockLLMClient` (so dev / CI runs without a
           secret do not 401).
        3. Otherwise delegate to :func:`app.llm.client.make_client`,
           which knows the OpenAI-format presets
           (``openai`` / ``deepseek`` / ``qwen``) plus ``anthropic``
           (routed through :class:`AnthropicClient`) and any extra
           OpenAI-compatible endpoint passed via ``AGENTFORGE_OPENAI_BASE_URL``.
        4. Each preset picks the right ``api_key``:
           ``openai``/``deepseek``/``qwen``/custom → ``openai_api_key``;
           ``anthropic`` → ``anthropic_api_key``.
        """
        import os

        from app.llm.client import make_client

        provider = (self.agent.provider or "openai").lower()
        env_provider = (os.environ.get("AGENTFORGE_LLM_PROVIDER") or "").lower()

        # ---- (1) explicit mock ------------------------------------------------
        if provider == "mock" or env_provider == "mock":
            return MockLLMClient(
                model=self.agent.model or self.settings.openai_default_model or "mock-model"
            )

        # ---- (2) any non-mock provider with no key → mock ---------------------
        # Avoids accidentally shipping a "placeholder" bearer that would 401.
        if provider == "anthropic":
            api_key = self.settings.anthropic_api_key
        else:
            api_key = self.settings.openai_api_key
        if not api_key:
            return MockLLMClient(
                model=self.agent.model or self.settings.openai_default_model or "mock-model"
            )

        # ---- (3) delegate to the preset table --------------------------------
        # `make_client` resolves `deepseek` / `qwen` / `openai` to the right
        # `base_url` + `default_model`, and `anthropic` to a dedicated
        # `AnthropicClient`.  For a known OpenAI preset we deliberately
        # **do not** pass `openai_base_url` — otherwise the OpenAI default
        # would silently override the preset endpoint.
        kwargs: dict[str, Any] = {
            "timeout": float(self.settings.request_timeout_seconds),
            "max_retries": self.settings.max_retries,
        }
        if provider not in {"openai", "deepseek", "qwen", "anthropic"}:
            # Unknown / custom → use the configured endpoint (which is
            # OpenAI-format by default, so this is a no-op for custom
            # OpenAI-compatible services).
            kwargs["base_url"] = self.settings.openai_base_url

        default_model = self.agent.model or (
            self.settings.anthropic_default_model
            if provider == "anthropic"
            else self.settings.openai_default_model
        )
        return make_client(
            provider=provider,
            api_key=api_key,
            model=default_model,
            **kwargs,
        )

    # ---- main entry point -------------------------------------------

    async def run(
        self,
        user_input: str,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        resume_state: Optional[RunState] = None,
    ) -> LoopResult:
        """Execute the agent loop.  See module docstring for details."""
        run = await self._get_or_create_run(
            run_id=run_id,
            user_input=user_input,
            session_id=session_id,
        )
        self._run = run

        # ---- context setup (with optional resume) ----
        if resume_state is not None:
            context = Context(
                max_tokens=int(self._agent_config.get("max_context_tokens", 8000)),
                model=self.agent.model or "gpt-4o-mini",
            )
            context.extend(resume_state.messages)
            start_step = resume_state.step_index
            consecutive_errors = resume_state.consecutive_errors
            total_prompt = resume_state.total_prompt_tokens
            total_completion = resume_state.total_completion_tokens
            output = resume_state.final_answer
            status = STATE_RUNNING
            error: Optional[str] = None
        else:
            context = self._injected_context or self._build_context(user_input)
            start_step = 1  # 1-based: first user-visible step is #1
            consecutive_errors = 0
            total_prompt = 0
            total_completion = 0
            output = ""
            status = RUN_STATUS_SUCCESS
            error = None

        max_steps = (
            self.config.max_steps
            or int(self.agent.max_steps or 20)
        )
        max_tokens = int(self._agent_config.get("max_context_tokens", 8000))
        model = self.config.model or self.agent.model or "gpt-4o-mini"
        temperature = (
            self.config.temperature
            if self.config.temperature is not None
            else float(self.agent.temperature if self.agent.temperature is not None else 0.7)
        )
        max_err = int(self.config.max_consecutive_errors or MAX_CONSECUTIVE_TOOL_ERRORS)

        steps: list[Step] = []
        start = time.perf_counter()
        paused = False

        # ---- main loop ----
        try:
            # step_idx is 1-based (e.g. #1, #2, ... #max_steps).
            # Both THINK and FINAL_ANSWER for a given LLM call share the
            # same step_idx -- the user sees them as one logical turn.
            for step_idx in range(start_step, max_steps + 1):
                if self._cancelled.is_set():
                    raise AgentCancelledError("run cancelled")

                # (c) step limit pre-check (we're still allowed one
                # more step at step_idx == max_steps).
                if step_idx > max_steps:
                    raise AgentTerminated("max_steps reached")

                # Token budget pre-check (enforced again inside _step_once)
                # (b) check token budget
                if context.total_tokens() > max_tokens:
                    context.enforce_budget()

                # (d) call LLM (+ optional tool execution)
                try:
                    outcome = await _step_once(
                        context=context,
                        llm=self.llm,
                        tool_exec=self.tool_executor,
                        tool_registry=self.tools,
                        enabled_tools=self._agent_tools or None,
                        model=model,
                        temperature=temperature,
                        max_context_tokens=max_tokens,
                        db_session=self.session,
                        agent_id=self.agent.id,
                    )
                except asyncio.TimeoutError as exc:
                    raise AgentTerminated(f"timeout: {exc}")
                except Exception as exc:  # noqa: BLE001
                    label, retryable = _classify_exception(exc)
                    status = RUN_STATUS_FAILED
                    error = f"{label}: {exc}"
                    await self._record_step(
                        run,
                        step_idx,
                        STEP_KIND_ERROR,
                        content=error,
                        status=STEP_STATUS_ERROR,
                        error=traceback.format_exc(limit=4),
                    )
                    raise

                response: LLMResponse = outcome["response"]
                total_prompt += response.prompt_tokens
                total_completion += response.completion_tokens

                # ---- record THINK step ----
                if response.content:
                    steps.append(
                        await self._record_step(
                            run,
                            step_idx,
                            STEP_KIND_THINK,
                            content=response.content,
                            payload={"tokens": response.completion_tokens},
                            duration_ms=outcome["duration_ms"],
                            tokens=response.completion_tokens,
                        )
                    )

                # ---- text-only -> done ----
                if not response.tool_calls:
                    output = response.content
                    steps.append(
                        await self._record_step(
                            run,
                            step_idx,
                            STEP_KIND_FINAL_ANSWER,
                            content=output,
                            payload={
                                "tokens": response.completion_tokens,
                                "prompt_tokens": response.prompt_tokens,
                            },
                            duration_ms=0,
                            tokens=response.completion_tokens,
                        )
                    )
                    status = RUN_STATUS_SUCCESS
                    break

                # ---- tool execution ----
                any_error = False
                for call, observation, call_err in outcome["tool_results"]:
                    t1 = time.perf_counter()
                    tool_ms = int((time.perf_counter() - t1) * 1000) or 1
                    if call_err is not None:
                        any_error = True
                        tool_status = STEP_STATUS_ERROR
                        err_text: Optional[str] = call_err
                    else:
                        tool_status = STEP_STATUS_OK
                        err_text = None

                    # tool_call step
                    steps.append(
                        await self._record_step(
                            run,
                            step_idx,
                            STEP_KIND_TOOL_CALL,
                            name=call.name,
                            content=call.arguments if isinstance(call.arguments, str) else json.dumps(
                                call.arguments, ensure_ascii=False, default=str
                            ),
                            payload={
                                "arguments": call.arguments,
                                "call_id": call.id,
                                # 0.1.3 #9 — risk badge in trace UI.  The
                                # tool's risk class is attached to every
                                # tool_call step's payload so the frontend
                                # ``StepNode`` can render a coloured
                                # warning badge (safe = no badge, read /
                                # write / network / exec = progressively
                                # louder).  Falls back to ``"safe"`` if
                                # the registry doesn't know the tool
                                # (e.g. for tools registered post-hoc via
                                # ``tool_metadata``).
                                "risk": self.tools.spec(call.name).risk
                                if self.tools.spec(call.name) is not None
                                else "safe",
                            },
                            duration_ms=tool_ms,
                            status=tool_status,
                            error=err_text,
                        )
                    )
                    # tool_result step
                    steps.append(
                        await self._record_step(
                            run,
                            step_idx,
                            STEP_KIND_TOOL_RESULT,
                            name=call.name,
                            content=observation,
                            payload={"call_id": call.id, "result": observation},
                            duration_ms=0,
                            status=STEP_STATUS_OK if not err_text else STEP_STATUS_ERROR,
                            error=err_text,
                        )
                    )
                    await self._maybe_save_memory(
                        observation, session_id=session_id
                    )

                # (f.2) 3 consecutive tool errors -> terminate
                if any_error:
                    consecutive_errors += 1
                    if consecutive_errors >= max_err:
                        status = RUN_STATUS_FAILED
                        error = (
                            f"aborted: {consecutive_errors} consecutive tool errors"
                        )
                        await self._record_step(
                            run,
                            step_idx,
                            STEP_KIND_OBSERVATION,
                            content=error,
                            status=STEP_STATUS_ERROR,
                        )
                        break
                else:
                    consecutive_errors = 0

                # ---- pause checkpoint ----
                if self._pause.is_set():
                    paused = True
                    status = RUN_STATUS_PAUSED
                    self._persist_state(
                        run,
                        context=context,
                        step_index=step_idx + 1,
                        consecutive_errors=consecutive_errors,
                        total_prompt=total_prompt,
                        total_completion=total_completion,
                        final_answer=output,
                    )
                    break

        except AgentCancelledError:
            status = RUN_STATUS_CANCELLED
            error = "cancelled"
        except AgentTerminated as exc:
            status = "terminated"
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            status = RUN_STATUS_FAILED
            error = f"{exc.__class__.__name__}: {exc}"
            logger.exception("Agent run failed: %s", exc)
            try:
                steps.append(
                    await self._record_step(
                        run,
                        len(steps),
                        STEP_KIND_OBSERVATION,
                        content=error,
                        status=STEP_STATUS_ERROR,
                        error=traceback.format_exc(limit=4),
                    )
                )
            except Exception:  # pragma: no cover
                pass

        duration_ms = int((time.perf_counter() - start) * 1000)
        run.status = status
        run.output = (output or error or "") if not paused else (output or "")
        run.error = error if not paused else None
        run.total_tokens = total_prompt + total_completion
        run.prompt_tokens = total_prompt
        run.completion_tokens = total_completion
        run.duration_ms = duration_ms
        run.step_count = len(steps)
        run.finished_at = datetime.utcnow() if not paused else None
        if not paused:
            # clear state on terminal status
            try:
                run.state = ""
            except Exception:  # pragma: no cover
                pass
        await _commit_with_retry(self.session)

        step_dicts = [_step_to_dict(s) for s in steps]
        return LoopResult(
            run_id=run.id,
            output=run.output or "",
            status=status,
            steps=step_dicts,
            total_tokens=run.total_tokens,
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
            duration_ms=run.duration_ms,
            step_count=run.step_count,
            error=error,
        )

    # ---- resume support ---------------------------------------------

    async def resume(self, run_id: str) -> LoopResult:
        """Resume a paused run by id."""
        run = await self.session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run {run_id!r} not found")
        if run.status != RUN_STATUS_PAUSED:
            raise RuntimeError(
                f"run {run_id!r} is not paused (status={run.status!r})"
            )
        state = RunState.from_json(run.state)
        # clear the pause flag and rerun
        self.resume_signal()
        return await self.run(
            user_input=state.task or run.input,
            run_id=run.id,
            session_id=run.session_id,
            resume_state=state,
        )

    def _persist_state(
        self,
        run: Run,
        *,
        context: Context,
        step_index: int,
        consecutive_errors: int,
        total_prompt: int,
        total_completion: int,
        final_answer: str,
    ) -> None:
        """Snapshot the live loop into a :class:`RunState`."""
        state = RunState(
            run_id=run.id,
            task=run.input,
            messages=list(context.to_openai()),
            step_index=step_index,
            consecutive_errors=consecutive_errors,
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            last_status=STATE_PAUSED,
            final_answer=final_answer,
        )
        # Persist the snapshot; the outer commit at end of run()
        # flushes this together with the rest of the row.
        try:
            run.state = state.to_json()
        except Exception:  # pragma: no cover
            logger.debug("Run.state column missing — pause state in-memory only")

    # ---- run/step persistence ---------------------------------------

    async def _get_or_create_run(
        self,
        *,
        run_id: Optional[str],
        user_input: str,
        session_id: Optional[str],
    ) -> Run:
        """Reuse a previously-persisted Run row when callers (e.g. the
        API) have already created one, otherwise insert a new one.
        """
        if run_id is not None:
            existing = await self.session.get(Run, run_id)
            if existing is not None:
                existing.input = user_input
                existing.status = RUN_STATUS_RUNNING
                existing.started_at = existing.started_at or datetime.utcnow()
                if session_id is not None:
                    existing.session_id = session_id
                await _commit_with_retry(self.session)
                return existing

        run = Run(
            id=run_id or str(uuid.uuid4()),
            agent_id=self.agent.id,
            input=user_input,
            status=RUN_STATUS_RUNNING,
            started_at=datetime.utcnow(),
            session_id=session_id,
        )
        self.session.add(run)
        await _commit_with_retry(self.session)
        return run

    def _build_context(self, user_input: str) -> Context:
        max_tokens = int(self._agent_config.get("max_context_tokens", 8000))
        ctx = Context(
            max_tokens=max_tokens,
            model=self.agent.model or "gpt-4o-mini",
        )
        if self.agent.system_prompt:
            ctx.add("system", self.agent.system_prompt)
        ctx.add("user", user_input)
        return ctx

    async def _record_step(
        self,
        run: Run,
        index: int,
        kind: str,
        *,
        content: str = "",
        name: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        duration_ms: int = 0,
        tokens: int = 0,
        status: str = STEP_STATUS_OK,
        error: Optional[str] = None,
    ) -> Step:
        step = Step(
            id=str(uuid.uuid4()),
            run_id=run.id,
            index=index,
            kind=kind,
            name=name,
            content=(content or "")[:8000],
            payload=json.dumps(payload or {}, ensure_ascii=False, default=str),
            duration_ms=duration_ms,
            tokens=tokens,
            status=status,
            error=error,
        )
        self.session.add(step)
        await _flush_with_retry(self.session)
        if self.config.on_step:
            try:
                self.config.on_step(step)
            except Exception:  # pragma: no cover
                logger.exception("on_step callback failed")
        return step

    async def _maybe_save_memory(
        self, observation: str, *, session_id: Optional[str]
    ) -> None:
        if len(observation) < 8 or len(observation) > 4000:
            return
        try:
            from app.agent.memory import MemoryStore

            store = MemoryStore(self.session, agent_id=self.agent.id)
            await store.add(
                observation,
                kind="episodic",
                importance=0.4,
                meta={"source": "tool_observation"},
                session_id=session_id,
            )
        except Exception:  # pragma: no cover
            logger.debug("memory save skipped", exc_info=True)


# ---------------------------------------------------------------------------
# Spec-style AgentLoopV2 — clean-room, no DB
# ---------------------------------------------------------------------------


class AgentLoopV2:
    """Spec-style loop.  Pure Python, no DB.

    Use this when you want to instantiate an agent without the ORM
    (e.g. unit tests, one-shot scripts, or embedded use).  For
    production runs persisted to PostgreSQL / SQLite, use
    :class:`AgentLoop`.

    The constructor matches the design spec::

        AgentLoop(config: AgentConfig,
                  llm_client: LLMClient,
                  tool_executor: ToolExecutor,
                  context_manager: ContextManager)
    """

    def __init__(
        self,
        config: AgentConfig,
        llm_client: LLMClient,
        tool_executor: ToolExecutor,
        context_manager: Context,
        *,
        tool_registry: Optional[ToolRegistry] = None,
    ) -> None:
        self.config = config
        self.llm = llm_client
        self.tool_executor = tool_executor
        self.context = context_manager
        # Registry is optional; if missing we pull it from the executor.
        self.tool_registry = tool_registry or getattr(
            tool_executor, "registry", default_registry
        )

    async def run(self, task: str) -> AgentRunResult:
        """Execute one agent loop and return an :class:`AgentRunResult`."""
        # 1. initialise context
        if not self.context.messages:
            if self.config.system_prompt:
                self.context.add("system", self.config.system_prompt)
            self.context.add("user", task)

        steps: list[TraceStep] = []
        consecutive_errors = 0
        total_prompt = 0
        total_completion = 0
        start = time.perf_counter()
        final_answer = ""
        status = "completed"
        error_message: Optional[str] = None

        try:
            for step_idx in range(int(self.config.max_steps or 20)):
                # (c) step limit
                if step_idx >= int(self.config.max_steps or 20):
                    status = "terminated"
                    error_message = "max_steps reached"
                    break

                # (b) token budget pre-check
                if self.context.total_tokens() > int(
                    self.config.max_context_tokens or 8000
                ):
                    self.context.enforce_budget()

                # (d) call LLM
                outcome = await _step_once(
                    context=self.context,
                    llm=self.llm,
                    tool_exec=self.tool_executor,
                    tool_registry=self.tool_registry,
                    enabled_tools=self.config.tools or None,
                    model=self.config.model,
                    temperature=float(self.config.temperature or 0.7),
                    max_context_tokens=int(
                        self.config.max_context_tokens or 8000
                    ),
                    db_session=None,
                    agent_id=None,
                )
                response: LLMResponse = outcome["response"]
                total_prompt += response.prompt_tokens
                total_completion += response.completion_tokens

                # (e.1) reasoning / think step
                if response.content:
                    steps.append(
                        TraceStep(
                            index=step_idx,
                            kind=STEP_KIND_THINK,
                            content=response.content,
                            payload={"tokens": response.completion_tokens},
                            duration_ms=outcome["duration_ms"],
                            tokens=response.completion_tokens,
                            created_at=datetime.utcnow(),
                        )
                    )

                # (e.2) text-only -> done
                if not response.tool_calls:
                    final_answer = response.content
                    steps.append(
                        TraceStep(
                            index=step_idx,
                            kind=STEP_KIND_FINAL_ANSWER,
                            content=final_answer,
                            payload={
                                "prompt_tokens": response.prompt_tokens,
                                "completion_tokens": response.completion_tokens,
                            },
                            tokens=response.completion_tokens,
                            created_at=datetime.utcnow(),
                        )
                    )
                    status = "completed"
                    break

                # (e.3) tool calls
                any_error = False
                for call, observation, call_err in outcome["tool_results"]:
                    if call_err is not None:
                        any_error = True
                        err_text: Optional[str] = call_err
                        step_status = STEP_STATUS_ERROR
                    else:
                        err_text = None
                        step_status = STEP_STATUS_OK

                    steps.append(
                        TraceStep(
                            index=step_idx,
                            kind=STEP_KIND_TOOL_CALL,
                            name=call.name,
                            content=json.dumps(
                                call.arguments, ensure_ascii=False, default=str
                            ),
                            # 0.1.3 #9 — risk badge in trace UI.  Same
                            # rationale as the production ``AgentLoop``
                            # code path: attach the tool's risk class to
                            # the step's payload so the frontend can
                            # render a coloured warning badge.
                            payload={
                                "arguments": call.arguments,
                                "call_id": call.id,
                                "risk": self.tool_registry.spec(call.name).risk
                                if self.tool_registry.spec(call.name) is not None
                                else "safe",
                            },
                            status=step_status,
                            error=err_text,
                            created_at=datetime.utcnow(),
                        )
                    )
                    steps.append(
                        TraceStep(
                            index=step_idx,
                            kind=STEP_KIND_TOOL_RESULT,
                            name=call.name,
                            content=observation,
                            payload={"call_id": call.id, "result": observation},
                            status=STEP_STATUS_ERROR if err_text else STEP_STATUS_OK,
                            error=err_text,
                            created_at=datetime.utcnow(),
                        )
                    )

                # (f.2) 3 consecutive errors -> terminate
                if any_error:
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                        status = "failed"
                        error_message = (
                            f"{consecutive_errors} consecutive tool errors"
                        )
                        break
                else:
                    consecutive_errors = 0
            else:
                # for-else: max_steps exhausted without a final answer
                status = "terminated"
                error_message = "max_steps reached"

        except asyncio.TimeoutError as exc:
            status = "failed"
            error_message = f"timeout: {exc}"
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            error_message = f"{exc.__class__.__name__}: {exc}"
            steps.append(
                TraceStep(
                    index=len(steps),
                    kind=STEP_KIND_ERROR,
                    content=str(exc),
                    error=traceback.format_exc(limit=4),
                    status=STEP_STATUS_ERROR,
                    created_at=datetime.utcnow(),
                )
            )

        total_latency_ms = int((time.perf_counter() - start) * 1000)
        return AgentRunResult(
            final_answer=final_answer,
            steps=steps,
            total_steps=len(steps),
            total_tokens=total_prompt + total_completion,
            total_latency_ms=total_latency_ms,
            status=status,
            error_message=error_message,
        )


# ---------------------------------------------------------------------------
# Adapter: build an AgentLoopV2 from an Agent ORM row (for tests / scripts)
# ---------------------------------------------------------------------------


def build_loop_v2_from_agent(
    agent: Agent,
    *,
    llm: Optional[LLMClient] = None,
    tool_executor: Optional[ToolExecutor] = None,
    context: Optional[Context] = None,
) -> AgentLoopV2:
    cfg = AgentConfig.from_orm(agent)
    if llm is None:
        llm = MockLLMClient(model=cfg.model)
    if tool_executor is None:
        tool_executor = ToolExecutor(default_registry)
    if context is None:
        context = Context(max_tokens=cfg.max_context_tokens, model=cfg.model)
    return AgentLoopV2(
        config=cfg,
        llm_client=llm,
        tool_executor=tool_executor,
        context_manager=context,
    )


__all__ = [
    # State constants
    "STATE_PENDING",
    "STATE_RUNNING",
    "STATE_PAUSED",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_CANCELLED",
    "STATE_TERMINATED",
    "MAX_CONSECUTIVE_TOOL_ERRORS",
    # Errors
    "AgentCancelledError",
    "AgentPausedError",
    "AgentTerminated",
    # Dataclasses
    "LoopResult",
    "LoopConfig",
    "TraceStep",
    "AgentRunResult",
    "AgentConfig",
    "RunState",
    "LLMResponse",
    "MockLLMClient",
    # Step kind / status re-exports (test contract)
    "STEP_KIND_THINK",
    "STEP_KIND_TOOL_CALL",
    "STEP_KIND_TOOL_RESULT",
    "STEP_KIND_FINAL_ANSWER",
    "STEP_KIND_OBSERVATION",
    "STEP_KIND_ERROR",
    "STEP_STATUS_OK",
    "STEP_STATUS_ERROR",
    # Classes
    "AgentLoop",
    "AgentLoopV2",
    "build_loop_v2_from_agent",
    # Helpers
    "_step_once",
    "_step_to_dict",
    "_classify_exception",
    "_safe_json_loads",
    "_stringify",
]


# ---------------------------------------------------------------------------
# Re-export step kind / status constants from app.models for test contract.
# The test_loop.py module imports these from app.agent.loop.
# ---------------------------------------------------------------------------
try:
    from app.models import (  # type: ignore
        STEP_KIND_THINK as _STEP_KIND_THINK,
        STEP_KIND_TOOL_CALL as _STEP_KIND_TOOL_CALL,
        STEP_KIND_TOOL_RESULT as _STEP_KIND_TOOL_RESULT,
        STEP_KIND_FINAL_ANSWER as _STEP_KIND_FINAL_ANSWER,
        STEP_KIND_OBSERVATION as _STEP_KIND_OBSERVATION,
        STEP_KIND_ERROR as _STEP_KIND_ERROR,
        STEP_STATUS_OK as _STEP_STATUS_OK,
        STEP_STATUS_ERROR as _STEP_STATUS_ERROR,
    )
    STEP_KIND_THINK = _STEP_KIND_THINK
    STEP_KIND_TOOL_CALL = _STEP_KIND_TOOL_CALL
    STEP_KIND_TOOL_RESULT = _STEP_KIND_TOOL_RESULT
    STEP_KIND_FINAL_ANSWER = _STEP_KIND_FINAL_ANSWER
    STEP_KIND_OBSERVATION = _STEP_KIND_OBSERVATION
    STEP_KIND_ERROR = _STEP_KIND_ERROR
    STEP_STATUS_OK = _STEP_STATUS_OK
    STEP_STATUS_ERROR = _STEP_STATUS_ERROR
except Exception:  # pragma: no cover
    # Fallback constants so this module still imports if app.models isn't loaded
    STEP_KIND_THINK = "think"
    STEP_KIND_TOOL_CALL = "tool_call"
    STEP_KIND_TOOL_RESULT = "tool_result"
    STEP_KIND_FINAL_ANSWER = "final_answer"
    STEP_KIND_OBSERVATION = "observation"
    STEP_KIND_ERROR = "error"
    STEP_STATUS_OK = "ok"
    STEP_STATUS_ERROR = "error"


# ---------------------------------------------------------------------------
# AgentLoopV2  --  test-friendly loop with explicit lifecycle / structured steps
# ---------------------------------------------------------------------------
# The original AgentLoop class is high-fidelity but tightly coupled to a
# specific Context/Step API.  Tests in test_loop.py use a much simpler
# contract:
#
#   cfg      = AgentConfig(...)
#   loop     = AgentLoopV2(cfg, llm, executor, context)
#   result   = await loop.run(prompt)
#   result   -> AgentRunResult(final_answer, status, total_steps, total_tokens,
#                              total_latency_ms, steps=[TraceStep, ...])
#
#   TraceStep = dataclass with .kind, .content, .status, .metadata
#
# `llm` may be a real LLMClient or a MockLLMClient that returns LLMResponse
# objects with .content and .tool_calls.
#
# `executor` is a ToolExecutor (from app.agent.tools) used to invoke tools.
#
# `context` is an app.agent.context.Context (or duck-typed) that exposes
#   .add_message(role, content)
#   .to_openai() -> list[dict]
#
# This class exists so that test_loop.py can drive the loop without
# monkey-patching the original AgentLoop internals.
# ---------------------------------------------------------------------------


@dataclass
class _V2Config:
    """Mirror of AgentConfig fields actually used by the loop."""

    model: str = "mock-model"
    temperature: float = 0.0
    max_steps: int = 20
    max_context_tokens: int = 4000
    system_prompt: str = "You are a helpful agent."

    @classmethod
    def from_agent_config(cls, cfg: AgentConfig) -> "_V2Config":
        return cls(
            model=cfg.model,
            temperature=cfg.temperature,
            max_steps=cfg.max_steps,
            max_context_tokens=cfg.max_context_tokens,
            system_prompt=cfg.system_prompt,
        )


class AgentLoopV2:
    """Simplified async loop that satisfies test_loop.py's contract.

    Lifecycle:
        loop = AgentLoopV2(config, llm, executor, context)
        await loop.run("user prompt") -> AgentRunResult
        # pause / resume hooks
        await loop.pause(); await loop.resume()
        # cancel
        await loop.cancel()
    """

    def __init__(
        self,
        config: AgentConfig,
        llm_client: Any,
        tool_executor: Any,
        context_manager: Any,
    ) -> None:
        # Accept either a real AgentConfig or any object with the same attrs.
        self.config = config
        self.llm = llm_client
        self.executor = tool_executor
        self.context = context_manager
        # Lifecycle flags
        self._cancelled = False
        self._paused = False
        # State defaults
        self.state = STATE_PENDING

    # ------------------------------------------------------------------ run
    async def run(self, prompt: str) -> AgentRunResult:
        """Drive the agent until it produces a final answer or terminates."""
        start = time.monotonic()
        steps: List[TraceStep] = []
        total_tokens = 0
        total_latency_ms = 0
        final_answer: Optional[str] = None
        status = STATE_RUNNING
        consecutive_tool_errors = 0

        # Seed the context with system + user message (if not already there).
        try:
            if self.config.system_prompt:
                self.context.add_message("system", self.config.system_prompt)
        except Exception:
            pass
        try:
            self.context.add_message("user", prompt)
        except Exception:
            pass

        self.state = STATE_RUNNING

        for step_index in range(self.config.max_steps):
            # ---- lifecycle gates ----------------------------------------
            if self._cancelled:
                status = STATE_CANCELLED
                break
            if self._paused:
                status = STATE_PAUSED
                break

            # ---- call the LLM -----------------------------------------
            t0 = time.monotonic()
            try:
                llm_resp: LLMResponse = await self.llm.complete(
                    messages=self._ctx_messages(),
                    model=self.config.model,
                    temperature=self.config.temperature,
                    tools=self._tool_specs(),
                )
            except Exception as exc:  # pragma: no cover
                steps.append(
                    TraceStep(
                        kind=STEP_KIND_ERROR,
                        content=f"LLM error: {exc}",
                        status=STEP_STATUS_ERROR,
                        metadata={"error": str(exc)},
                    )
                )
                status = STATE_FAILED
                break
            t1 = time.monotonic()
            latency_ms = int((t1 - t0) * 1000)
            total_latency_ms += latency_ms

            # Count tokens
            try:
                tok = int(getattr(llm_resp, "usage", {}).get("total_tokens", 0) or 0)
            except Exception:
                tok = 0
            total_tokens += tok

            # Append the assistant message into the context for the next round.
            try:
                if llm_resp.content is not None:
                    self.context.add_message("assistant", llm_resp.content)
            except Exception:
                pass

            # Think step (the model's textual reasoning, if any)
            if llm_resp.content:
                steps.append(
                    TraceStep(
                        kind=STEP_KIND_THINK,
                        content=llm_resp.content,
                        status=STEP_STATUS_OK,
                        metadata={"step": step_index},
                    )
                )

            # ---- no tool calls => final answer -------------------------
            tool_calls = getattr(llm_resp, "tool_calls", None) or []
            if not tool_calls:
                final_answer = llm_resp.content or ""
                # Synthesize a final_answer step for visibility
                if final_answer:
                    steps.append(
                        TraceStep(
                            kind=STEP_KIND_FINAL_ANSWER,
                            content=final_answer,
                            status=STEP_STATUS_OK,
                            metadata={"step": step_index},
                        )
                    )
                status = STATE_COMPLETED
                break

            # ---- execute each tool call --------------------------------
            had_error_this_round = False
            for tc in tool_calls:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                args = (
                    tc.get("arguments")
                    if isinstance(tc, dict)
                    else getattr(tc, "arguments", None)
                )
                if args is None:
                    args = {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"_raw": args}
                tc_id = (
                    tc.get("id")
                    if isinstance(tc, dict)
                    else getattr(tc, "id", None) or f"call_{step_index}"
                )

                # Special built-in tool: 'final_answer' is how some agents
                # signal completion even when tool_calls is present.
                if name == "final_answer":
                    fa = args.get("answer") if isinstance(args, dict) else str(args)
                    final_answer = str(fa)
                    steps.append(
                        TraceStep(
                            kind=STEP_KIND_FINAL_ANSWER,
                            content=str(fa),
                            status=STEP_STATUS_OK,
                            metadata={"tool_call_id": tc_id},
                        )
                    )
                    continue

                # Record the tool call itself
                steps.append(
                    TraceStep(
                        kind=STEP_KIND_TOOL_CALL,
                        content=json.dumps({"name": name, "arguments": args}, ensure_ascii=False),
                        status=STEP_STATUS_OK,
                        metadata={"tool": name, "tool_call_id": tc_id, "step": step_index},
                    )
                )

                # Execute the tool (executor is async)
                t_exec_0 = time.monotonic()
                tool_result: Any = None
                tool_ok = True
                try:
                    tool_result = await self.executor.execute(name, args)
                except Exception as exc:
                    tool_result = f"ERROR: {exc}"
                    tool_ok = False
                t_exec_1 = time.monotonic()
                total_latency_ms += int((t_exec_1 - t_exec_0) * 1000)

                # Serialize the result to a string for the trace and for the
                # tool message that will be fed back to the LLM.
                if isinstance(tool_result, str):
                    tool_result_str = tool_result
                else:
                    try:
                        tool_result_str = json.dumps(
                            tool_result, ensure_ascii=False, default=str
                        )
                    except Exception:
                        tool_result_str = str(tool_result)

                # Record the tool result / observation
                result_kind = STEP_KIND_TOOL_RESULT if tool_ok else STEP_KIND_ERROR
                result_status = STEP_STATUS_OK if tool_ok else STEP_STATUS_ERROR
                steps.append(
                    TraceStep(
                        kind=result_kind,
                        content=str(tool_result_str),
                        status=result_status,
                        metadata={"tool": name, "tool_call_id": tc_id, "step": step_index},
                    )
                )

                # Track consecutive tool errors for safety
                if tool_ok:
                    consecutive_tool_errors = 0
                else:
                    consecutive_tool_errors += 1

                if consecutive_tool_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                    steps.append(
                        TraceStep(
                            kind=STEP_KIND_ERROR,
                            content=(
                                f"Aborting: {MAX_CONSECUTIVE_TOOL_ERRORS} consecutive "
                                "tool errors."
                            ),
                            status=STEP_STATUS_ERROR,
                            metadata={"consecutive_tool_errors": consecutive_tool_errors},
                        )
                    )
                    status = STATE_TERMINATED
                    break

                # Feed observation back into context for the next LLM call
                try:
                    self.context.add_message(
                        "tool", str(tool_result_str), tool_call_id=tc_id, name=name
                    )
                except TypeError:
                    # Some Context impls don't accept kwargs
                    try:
                        self.context.add_message("tool", str(tool_result_str))
                    except Exception:
                        pass
                except Exception:
                    pass

                if had_error_this_round:
                    continue

            if status in (STATE_TERMINATED, STATE_FAILED, STATE_CANCELLED, STATE_PAUSED):
                break

            if final_answer is not None and status == STATE_COMPLETED:
                break

        # If we ran out of steps without a final answer, mark as terminated.
        if final_answer is None and status in (STATE_RUNNING,):
            status = STATE_TERMINATED

        if final_answer is None:
            final_answer = ""

        total = int((time.monotonic() - start) * 1000)
        self.state = status

        return AgentRunResult(
            final_answer=final_answer,
            status=status,
            total_steps=len(steps),
            total_tokens=total_tokens,
            total_latency_ms=total,
            steps=steps,
        )

    # ------------------------------------------------------------------ helpers
    def _ctx_messages(self) -> List[Dict[str, Any]]:
        try:
            return list(self.context.to_openai())
        except Exception:
            # Fall back to whatever attribute exists
            msgs = getattr(self.context, "messages", None) or []
            return list(msgs)

    def _tool_specs(self) -> List[Dict[str, Any]]:
        try:
            specs = self.executor.openai_tools()  # type: ignore[attr-defined]
            if specs:
                return list(specs)
        except Exception:
            pass
        # Try the registry directly
        try:
            reg = getattr(self.executor, "registry", None)
            if reg is not None and hasattr(reg, "openai_tools"):
                return list(reg.openai_tools())
        except Exception:
            pass
        return []

    # ------------------------------------------------------------------ control
    async def pause(self) -> None:
        self._paused = True
        self.state = STATE_PAUSED

    async def resume(self) -> None:
        self._paused = False
        self.state = STATE_RUNNING

    async def cancel(self) -> None:
        self._cancelled = True
        self.state = STATE_CANCELLED
        raise AgentCancelledError("Agent run cancelled")

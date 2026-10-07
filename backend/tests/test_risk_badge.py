"""Tests for the tool risk badge in the trace UI (0.1.3 #9).

The risk class of a tool (``safe`` / ``read`` / ``write`` / ``network``
/ ``exec``) is recorded in the ``payload["risk"]`` of every
``tool_call`` step by :class:`app.agent.loop.AgentLoop` and surfaced to
the frontend through:

1. :class:`app.api.schemas.StepOut` (Pydantic computed field)
2. :func:`app.api.runs._serialize_step` (the REST ``GET /runs/{id}`` path)
3. :func:`app.agent.loop._step_to_dict` (the WebSocket ``run.step`` path)

These tests pin the contract at all three boundaries so a regression
on any one path is caught immediately.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.agent.loop import _step_to_dict
from app.agent.tools import ToolRegistry
from app.api.runs import _serialize_step
from app.api.schemas import StepOut


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_step(
    kind: str = "tool_call",
    name: str = "shell_exec",
    payload: dict | None = None,
):
    """Build a duck-typed Step object (avoids needing a real DB row).

    The ``payload`` attribute is a JSON string because that is what the
    SQLAlchemy ``Step.payload`` column stores and what the REST
    serializer consumes.
    """
    if payload is None:
        payload = {"arguments": {"command": "ls"}, "risk": "exec"}
    return SimpleNamespace(
        id="s1",
        index=1,
        kind=kind,
        name=name,
        content="",
        payload=json.dumps(payload, ensure_ascii=False),
        duration_ms=10,
        tokens=0,
        status="ok",
        error=None,
        created_at=None,
    )


def _to_pydantic_step(step):
    """Flatten a duck-typed ``Step`` into a dict that ``StepOut`` can
    validate.  The Pydantic schema expects ``payload`` to be a dict;
    the SQLAlchemy column stores a JSON string, so we deserialise.
    """
    raw = step.payload
    if isinstance(raw, str):
        try:
            payload = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            payload = {"raw": raw}
    else:
        payload = raw or {}
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
        "created_at": step.created_at,
    }


# ---------------------------------------------------------------------------
# 1. StepOut computed field
# ---------------------------------------------------------------------------


def test_stepout_risk_for_tool_call():
    """A ``tool_call`` step exposes its risk class on the schema."""
    out = StepOut.model_validate(_to_pydantic_step(_payload_step()))
    assert out.risk == "exec"
    assert out.kind == "tool_call"
    # The underlying payload keeps the raw value too (backward compat
    # for any consumer that still reads it directly).
    assert out.payload["risk"] == "exec"


def test_stepout_risk_none_for_non_tool_step():
    """``think`` / ``final_answer`` etc. never get a risk badge."""
    out = StepOut.model_validate(
        _to_pydantic_step(_payload_step(kind="think", name="llm"))
    )
    assert out.risk is None

    out2 = StepOut.model_validate(
        _to_pydantic_step(_payload_step(kind="final_answer"))
    )
    assert out2.risk is None


def test_stepout_risk_handles_legacy_payload():
    """Pre-0.1.3 rows had no ``payload["risk"]`` — return ``None``."""
    legacy = _payload_step(name="echo", payload={"arguments": {"text": "hi"}})
    out = StepOut.model_validate(_to_pydantic_step(legacy))
    assert out.risk is None


def test_stepout_risk_handles_empty_payload():
    """A row with an empty payload returns ``None``, not a crash."""
    empty = SimpleNamespace(
        id="s3",
        index=1,
        kind="tool_call",
        name="echo",
        content="",
        payload="",
        duration_ms=1,
        tokens=0,
        status="ok",
        error=None,
        created_at=None,
    )
    out = StepOut.model_validate(_to_pydantic_step(empty))
    assert out.risk is None


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 2. _serialize_step (REST path used by GET /runs/{id})
# ---------------------------------------------------------------------------


def test_serialize_step_includes_risk_for_tool_call():
    """The REST serializer surfaces ``risk`` on the wire."""
    serialized = _serialize_step(_payload_step())
    assert serialized["risk"] == "exec"
    assert serialized["kind"] == "tool_call"


def test_serialize_step_omits_risk_for_non_tool_step():
    """Non-tool steps get ``risk=None`` rather than a misleading badge."""
    serialized = _serialize_step(_payload_step(kind="think"))
    assert serialized["risk"] is None


# ---------------------------------------------------------------------------
# 3. _step_to_dict (WebSocket run.step event path)
# ---------------------------------------------------------------------------


def test_step_to_dict_includes_risk_for_tool_call():
    """The WS event shape mirrors the REST shape so the frontend can
    use a single type for both ingestion paths."""
    d = _step_to_dict(_payload_step())
    assert d["risk"] == "exec"


def test_step_to_dict_omits_risk_for_non_tool_step():
    d = _step_to_dict(_payload_step(kind="observation"))
    assert d["risk"] is None


# ---------------------------------------------------------------------------
# 4. Sanity: registry actually carries the right risk classes.
# ---------------------------------------------------------------------------
# Guards against a future refactor that drops the ``risk=`` kw from a
# built-in tool's @tool decorator — the trace UI would silently start
# reporting "safe" for everything.


def test_registry_risk_for_known_tools():
    assert ToolRegistry.spec("echo").risk == "safe"
    assert ToolRegistry.spec("now").risk == "safe"
    assert ToolRegistry.spec("final_answer").risk == "safe"
    assert ToolRegistry.spec("calculator").risk == "safe"
    assert ToolRegistry.spec("http_request").risk == "network"
    assert ToolRegistry.spec("shell_exec").risk == "exec"
    assert ToolRegistry.spec("file_read").risk == "read"
    assert ToolRegistry.spec("file_write").risk == "write"


def test_registry_risk_for_unknown_tool_is_none():
    """``spec`` returns ``None`` for an unknown name (so the loop's
    ``is not None`` guard falls back to ``safe``)."""
    assert ToolRegistry.spec("definitely_not_a_tool") is None


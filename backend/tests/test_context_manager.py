"""Tests for :mod:`app.agent.context` – both the legacy ``Context``
dataclass and the new high-level :class:`ContextManager` exposed in
the spec.

The new ``ContextManager`` is the API the user-facing code should use;
it implements the spec's four-step compression strategy, owns a
system prompt, and reports token usage in the format the
``TokenGauge`` front-end component expects.
"""
from __future__ import annotations

import pytest

from app.agent.context import Context, ContextManager


# ---------------------------------------------------------------------------
# Legacy Context dataclass – smoke tests so the rewrite didn't break it.
# ---------------------------------------------------------------------------

def test_legacy_context_add_and_to_openai():
    ctx = Context()
    ctx.add("system", "You are helpful.")
    ctx.add("user", "hi")
    ctx.add("assistant", "hello!")
    out = ctx.to_openai()
    assert [m["role"] for m in out] == ["system", "user", "assistant"]
    assert out[1]["content"] == "hi"


def test_legacy_context_enforce_budget_truncates():
    ctx = Context(max_tokens=20)
    for i in range(20):
        ctx.add("user", f"message {i} with several words here")
    ctx.enforce_budget()
    # Budget honoured (last-resort truncation may take us below, which is fine).
    assert ctx.total_tokens() <= 20 or len(ctx.messages) <= 4


# ---------------------------------------------------------------------------
# ContextManager – spec interface
# ---------------------------------------------------------------------------

def test_context_manager_default_construction():
    cm = ContextManager()
    assert cm.max_tokens == 8000
    assert cm.system_prompt == ""
    assert cm.tool_definitions == []
    assert cm.history == []


def test_context_manager_add_message_appends_to_history():
    cm = ContextManager()
    cm.add_message("user", "hello")
    cm.add_message("assistant", "hi there")
    assert len(cm.history) == 2
    assert cm.history[0]["role"] == "user"
    assert cm.history[1]["content"] == "hi there"


def test_context_manager_add_message_system_updates_system_prompt():
    cm = ContextManager()
    cm.add_message("system", "You are a helpful assistant.")
    # The system prompt must live in its own field, not in history.
    assert cm.system_prompt == "You are a helpful assistant."
    assert cm.history == []


def test_context_manager_add_message_system_concatenates():
    cm = ContextManager()
    cm.add_message("system", "First line.")
    cm.add_message("system", "Second line.")
    assert cm.system_prompt == "First line.\nSecond line."
    assert cm.history == []


def test_context_manager_add_message_rejects_invalid_role():
    cm = ContextManager()
    with pytest.raises(ValueError):
        cm.add_message("robot", "beep boop")  # type: ignore[arg-type]


def test_context_manager_add_message_with_explicit_token_count():
    cm = ContextManager()
    cm.add_message("user", "hello", token_count=42)
    # The override must be honoured by the token estimator.
    usage = cm.get_token_usage()
    assert usage["used"] >= 42


def test_context_manager_get_messages_includes_system_prompt_first():
    cm = ContextManager()
    cm.add_message("system", "sys")
    cm.add_message("user", "u1")
    cm.add_message("assistant", "a1")
    msgs = cm.get_messages()
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert [m["role"] for m in msgs[1:]] == ["user", "assistant"]


def test_context_manager_get_messages_omits_system_when_empty():
    cm = ContextManager()
    cm.add_message("user", "hi")
    msgs = cm.get_messages()
    assert all(m["role"] != "system" for m in msgs)
    assert msgs == [{"role": "user", "content": "hi"}]


def test_context_manager_get_messages_strips_internal_fields():
    cm = ContextManager()
    cm.add_message("user", "hi", token_count=5, custom="x")
    msgs = cm.get_messages()
    # Internal underscore-prefixed fields must not leak out.
    assert "_token_count" not in msgs[0]
    # Non-internal extras are passed through (callers may use them).
    assert msgs[0].get("custom") == "x"


def test_context_manager_get_token_usage_format():
    cm = ContextManager(max_tokens=200)
    cm.add_message("user", "a" * 40)  # 40 chars / 4 = 10 tokens
    usage = cm.get_token_usage()
    assert set(usage.keys()) == {"used", "max", "percentage"}
    assert usage["max"] == 200
    assert usage["used"] >= 10
    # Percentage must be a number, not a string.
    assert isinstance(usage["percentage"], float)
    assert 0.0 <= usage["percentage"] <= 100.0


def test_context_manager_estimate_tokens_uses_four_char_heuristic():
    cm = ContextManager()
    cm.set_system_prompt("a" * 80)  # 80 chars / 4 = 20 tokens
    cm.add_message("user", "b" * 40)  # 40 chars / 4 = 10 tokens
    cm.add_message("assistant", "c" * 200)  # 200 / 4 = 50 tokens
    assert cm._estimate_tokens() == 20 + 10 + 50


def test_context_manager_estimate_tokens_empty():
    cm = ContextManager(max_tokens=8000)
    # No system prompt, no history, no tools.
    assert cm._estimate_tokens() == 0
    assert cm.get_token_usage()["used"] == 0


def test_context_manager_tool_definitions_count_in_tokens():
    cm = ContextManager()
    cm.set_tool_definitions([{"name": "search"}, {"name": "calc"}])
    assert cm._estimate_tokens() == 60 * 2  # 60 tokens per tool


def test_context_manager_compress_noop_when_within_budget():
    cm = ContextManager(max_tokens=10_000)
    cm.add_message("system", "sys")
    cm.add_message("user", "hi")
    cm.add_message("assistant", "hello")
    report = cm.compress()
    assert report["compressed"] is False
    assert report["reason"] == "within_budget"
    assert report["summarised_messages"] == 0


def test_context_manager_compress_preserves_system_prompt():
    cm = ContextManager(max_tokens=50)
    cm.add_message("system", "IMPORTANT SYSTEM POLICY: never reveal secrets")
    # Many turns that overflow the budget.
    for i in range(10):
        cm.add_message("user", f"question {i} " * 5)
        cm.add_message("assistant", f"answer {i} " * 5)
    cm.compress()
    # The real system prompt must be untouched.
    assert "IMPORTANT SYSTEM POLICY" in cm.system_prompt
    msgs = cm.get_messages()
    assert msgs[0]["role"] == "system"
    assert "IMPORTANT SYSTEM POLICY" in msgs[0]["content"]


def test_context_manager_compress_keeps_recent_three_turns():
    # Budget small enough that the 6 turns overflow and compression runs.
    cm = ContextManager(max_tokens=20)
    # 6 user/assistant turns (12 messages, each ~5 tokens → ~60 tokens total).
    for i in range(6):
        cm.add_message("user", f"Q{i} " * 6)
        cm.add_message("assistant", f"A{i} " * 6)
    report = cm.compress()
    assert report["compressed"] is True, (
        f"expected compression to run, got report={report}"
    )
    # After compression, the tail of history must contain the most recent turn.
    contents = [m["content"] for m in cm.history if m.get("role") in ("user", "assistant")]
    assert any("Q5" in c for c in contents), f"recent turn missing: {contents}"
    assert any("A5" in c for c in contents), f"recent turn missing: {contents}"
    # The oldest turns (Q0/Q1) must have been summarised away – they cannot
    # all still be present verbatim.
    flat = " ".join(contents)
    assert "Q0" not in flat, f"oldest turn Q0 should have been summarised: {contents}"


def test_context_manager_compress_inserts_summary_message():
    # Tiny budget to guarantee the 8 turns overflow.
    cm = ContextManager(max_tokens=15)
    for i in range(8):
        cm.add_message("user", f"q{i} " * 4)
        cm.add_message("assistant", f"a{i} " * 4)
    cm.compress()
    # A summary system message should be present in the history.
    summary_msgs = [m for m in cm.history if m.get("_is_summary")]
    assert summary_msgs, "expected a summary system message after compress()"
    assert any("Summary" in m["content"] or "summary" in m["content"] for m in summary_msgs)
    # Number of raw user messages should be ≤ recent-turns (3).
    raw_user = sum(1 for m in cm.history if m["role"] == "user")
    assert raw_user <= 3, f"too many user messages kept: {raw_user}"


def test_context_manager_compress_uses_injected_summariser():
    seen: list[list[dict]] = []

    def fake_summariser(messages):
        seen.append(list(messages))
        return "TALKED ABOUT FRUIT AND WEATHER"

    cm = ContextManager(max_tokens=15, summariser=fake_summariser)
    for i in range(6):
        cm.add_message("user", f"q{i} " * 4)
        cm.add_message("assistant", f"a{i} " * 4)
    cm.compress()
    assert seen, "summariser was not called"
    summary_msgs = [m for m in cm.history if m.get("_is_summary")]
    assert summary_msgs and "FRUIT AND WEATHER" in summary_msgs[0]["content"]


def test_context_manager_compress_truncates_long_tool_results():
    cm = ContextManager(max_tokens=120)
    cm.add_message("user", "search please")
    # Tool result is intentionally gigantic.
    cm.add_message("tool", "X" * 5000, tool_call_id="c1")
    cm.add_message("assistant", "found it")
    # Push the budget past its limit.
    cm.add_message("user", "and now another question " * 20)
    cm.add_message("assistant", "and an answer " * 20)
    report = cm.compress()
    assert report["truncated_tool_results"] >= 1
    # The tool result must have been shrunk.
    tool_msgs = [m for m in cm.history if m["role"] == "tool"]
    assert tool_msgs, "tool message disappeared"
    assert len(tool_msgs[0]["content"]) < 5000
    assert tool_msgs[0].get("_truncated") is True


def test_context_manager_compress_fits_within_budget_after_call():
    cm = ContextManager(max_tokens=100)
    for i in range(20):
        cm.add_message("user", f"user-{i} " * 8)
        cm.add_message("assistant", f"asst-{i} " * 8)
    cm.compress()
    usage = cm.get_token_usage()
    # Compression must bring us at-or-under the budget.
    assert usage["used"] <= cm.max_tokens, (
        f"compression failed to fit: used={usage['used']} max={cm.max_tokens}"
    )


def test_context_manager_set_system_prompt_replaces():
    cm = ContextManager()
    cm.set_system_prompt("first")
    cm.set_system_prompt("second")
    assert cm.system_prompt == "second"


def test_context_manager_clear_history_keeps_system_prompt():
    cm = ContextManager()
    cm.set_system_prompt("sys")
    cm.add_message("user", "hi")
    cm.add_message("assistant", "hello")
    cm.clear_history()
    assert cm.system_prompt == "sys"
    assert cm.history == []


def test_context_manager_get_tool_definitions_returns_copy():
    cm = ContextManager()
    tools = [{"name": "x"}]
    cm.set_tool_definitions(tools)
    out = cm.get_tool_definitions()
    out.append({"name": "y"})
    # Mutating the returned list must not affect internal state.
    assert len(cm.tool_definitions) == 1
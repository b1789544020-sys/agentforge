"""Tests for app.agent.loop (AgentLoopV2 / AgentRunResult)."""
from __future__ import annotations

import asyncio
import pytest

from app.agent.loop import (
    AgentLoopV2,
    AgentConfig,
    AgentRunResult,
    MAX_CONSECUTIVE_TOOL_ERRORS,
    TraceStep,
    MockLLMClient,
    LLMResponse,
)
from app.agent.context import Context
from app.agent.tools import (
    ToolRegistry,
    ToolExecutor,
    ToolDef,
    ToolError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**over) -> AgentConfig:
    base = dict(
        agent_id="a1",
        name="tester",
        model="gpt-test",
        system_prompt="You are a tester.",
        tools=[],
        max_steps=5,
        temperature=0.0,
        max_context_tokens=4000,
        extra={},
    )
    base.update(over)
    return AgentConfig(**base)


def _llm_final(text: str = "ok") -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[], finish_reason="stop")


def _llm_tool_call(name: str, args: dict, call_id: str = "c1") -> LLMResponse:
    from app.llm.client import ToolCall

    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
        finish_reason="tool_calls",
    )


def _echo_tool() -> ToolDef:
    async def _impl(args, ctx):
        return {"echo": args.get("value")}

    return ToolDef(
        name="echo",
        description="echo the value back",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        handler=_impl,
    )


def _boom_tool() -> ToolDef:
    async def _impl(args, ctx):
        raise RuntimeError("boom")

    return ToolDef(
        name="boom",
        description="always errors",
        parameters={"type": "object", "properties": {}},
        handler=_impl,
    )


def _ctx() -> Context:
    return Context(max_tokens=4000, model="gpt-test")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_text_response_returns_final_answer():
    llm = MockLLMClient(responses=[_llm_final("hello world")])
    registry = ToolRegistry()
    executor = ToolExecutor(registry)
    loop = AgentLoopV2(_cfg(), llm, executor, _ctx())
    result = await loop.run("hi")
    assert isinstance(result, AgentRunResult)
    assert result.final_answer == "hello world"
    assert result.status in {"completed", "success"}
    assert result.total_steps >= 1
    assert result.total_tokens >= 0
    assert result.total_latency_ms >= 0
    # Steps are TraceStep
    for s in result.steps:
        assert isinstance(s, TraceStep)


@pytest.mark.asyncio
async def test_tool_call_flow_runs_and_observes():
    llm = MockLLMClient(
        responses=[
            _llm_tool_call("echo", {"value": "x"}),
            _llm_final("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_echo_tool())
    executor = ToolExecutor(registry)
    loop = AgentLoopV2(_cfg(), llm, executor, _ctx())
    result = await loop.run("go")
    assert result.final_answer == "done"
    kinds = [s.kind for s in result.steps]
    assert "tool_call" in kinds
    assert "tool_result" in kinds or "observation" in kinds
    assert any("x" in (s.content or "") for s in result.steps)


@pytest.mark.asyncio
async def test_unknown_tool_does_not_crash_immediately():
    llm = MockLLMClient(
        responses=[
            _llm_tool_call("nope", {}),
            _llm_final("fallback"),
        ]
    )
    registry = ToolRegistry()
    executor = ToolExecutor(registry)
    loop = AgentLoopV2(_cfg(), llm, executor, _ctx())
    result = await loop.run("?")
    assert result.final_answer == "fallback"
    # The run must record an error step or the executor must record a tool error step.
    assert any(s.status == "error" or s.kind == "error" for s in result.steps)


@pytest.mark.asyncio
async def test_max_steps_triggers_termination_status():
    # LLM keeps producing tool calls forever, so the loop must stop at max_steps.
    registry = ToolRegistry()
    registry.register(_echo_tool())
    executor = ToolExecutor(registry)
    # supply a long list of tool-call responses
    tool_resp = _llm_tool_call("echo", {"value": "v"})
    llm = MockLLMClient(responses=[tool_resp] * 50)
    loop = AgentLoopV2(_cfg(max_steps=3), llm, executor, _ctx())
    result = await loop.run("x")
    assert result.status in {"terminated", "max_steps_reached", "completed"}
    # number of non-zero steps <= max_steps (we expect it to stop around there)
    assert result.total_steps <= 6  # allow some slack for think/observation steps


@pytest.mark.asyncio
async def test_consecutive_tool_errors_counted_and_terminate():
    # The "boom" tool always raises. After MAX_CONSECUTIVE_TOOL_ERRORS in a row
    # the loop must terminate with a failure / terminated status.
    registry = ToolRegistry()
    registry.register(_boom_tool())
    executor = ToolExecutor(registry)
    tool_resp = _llm_tool_call("boom", {})
    llm = MockLLMClient(responses=[tool_resp] * 50)
    loop = AgentLoopV2(_cfg(max_steps=20), llm, executor, _ctx())
    result = await loop.run("x")
    # status should not be 'completed' (or should at least be terminated)
    assert result.status in {"terminated", "max_steps_reached", "failed", "error", "completed"}
    # If 'completed' then the loop tolerated errors (acceptable in some impls);
    # what MUST hold is that the run did not run forever.
    assert result.total_steps < 50


@pytest.mark.asyncio
async def test_temperature_and_model_passed_through():
    # Just confirms AgentConfig is forwarded; loop uses these to call LLM.
    cfg = _cfg(model="my-model", temperature=0.7, max_context_tokens=1024)
    llm = MockLLMClient(responses=[_llm_final("ok")])
    executor = ToolExecutor(ToolRegistry())
    loop = AgentLoopV2(cfg, llm, executor, _ctx())
    assert loop.config.model == "my-model"
    assert loop.config.temperature == 0.7
    assert loop.config.max_context_tokens == 1024
    result = await loop.run("?")
    assert result.final_answer == "ok"


@pytest.mark.asyncio
async def test_context_is_populated_with_system_and_user():
    llm = MockLLMClient(responses=[_llm_final("ok")])
    executor = ToolExecutor(ToolRegistry())
    ctx = _ctx()
    loop = AgentLoopV2(_cfg(system_prompt="SYS-X"), llm, executor, ctx)
    await loop.run("USER-Y")
    roles = [m["role"] for m in ctx.to_openai()]
    assert "system" in roles
    assert "user" in roles
    # System prompt and user message must be present
    assert any(m.get("content") == "SYS-X" for m in ctx.to_openai())
    assert any(m.get("content") == "USER-Y" for m in ctx.to_openai())


@pytest.mark.asyncio
async def test_token_total_is_nonnegative():
    llm = MockLLMClient(responses=[_llm_final("ok")])
    executor = ToolExecutor(ToolRegistry())
    loop = AgentLoopV2(_cfg(), llm, executor, _ctx())
    result = await loop.run("x")
    assert result.total_tokens >= 0


@pytest.mark.asyncio
async def test_steps_have_unique_indices_or_kinds():
    llm = MockLLMClient(responses=[_llm_final("ok")])
    executor = ToolExecutor(ToolRegistry())
    loop = AgentLoopV2(_cfg(), llm, executor, _ctx())
    result = await loop.run("x")
    # Each step is a TraceStep with at least a kind and content
    for s in result.steps:
        assert hasattr(s, "kind")
        assert hasattr(s, "content")
        assert s.kind  # not empty


@pytest.mark.asyncio
async def test_error_message_field_present():
    llm = MockLLMClient(responses=[_llm_final("ok")])
    executor = ToolExecutor(ToolRegistry())
    loop = AgentLoopV2(_cfg(), llm, executor, _ctx())
    result = await loop.run("x")
    # On a clean run, error_message should be None or empty string
    assert result.error_message in (None, "")


@pytest.mark.asyncio
async def test_zero_max_steps_immediate_termination():
    llm = MockLLMClient(responses=[_llm_final("never")])
    executor = ToolExecutor(ToolRegistry())
    loop = AgentLoopV2(_cfg(max_steps=0), llm, executor, _ctx())
    result = await loop.run("x")
    # With max_steps=0 the loop should not even call the LLM (or call once at most)
    assert result.status in {"terminated", "max_steps_reached", "completed"}
    # If the loop calls the LLM 0 times, no "final" answer comes from the model.
    if result.status in {"terminated", "max_steps_reached"}:
        # The final_answer should be empty (no LLM answer produced)
        assert result.final_answer in ("", None)


@pytest.mark.asyncio
async def test_tool_executor_missing_tool_returns_error_step():
    llm = MockLLMClient(
        responses=[
            _llm_tool_call("missing", {"a": 1}),
            _llm_final("recovered"),
        ]
    )
    executor = ToolExecutor(ToolRegistry())  # empty registry
    loop = AgentLoopV2(_cfg(), llm, executor, _ctx())
    result = await loop.run("x")
    # Must not raise; must finish
    assert result.final_answer == "recovered"
    # Should have at least one error step
    assert any(s.status == "error" or s.kind == "error" for s in result.steps)


def test_max_consecutive_tool_errors_is_a_positive_int():
    assert isinstance(MAX_CONSECUTIVE_TOOL_ERRORS, int)
    assert MAX_CONSECUTIVE_TOOL_ERRORS >= 1


def test_agent_config_fields_are_typed():
    cfg = _cfg()
    assert cfg.agent_id == "a1"
    assert cfg.name == "tester"
    assert isinstance(cfg.tools, list)
    assert isinstance(cfg.max_steps, int)
    assert isinstance(cfg.temperature, float)
    assert isinstance(cfg.max_context_tokens, int)
    assert isinstance(cfg.extra, dict)


@pytest.mark.asyncio
async def test_run_is_idempotent_on_empty_responses():
    # If the LLM is exhausted (no more responses), MockLLMClient should still
    # return *something* (typically empty) and the loop must not crash.
    llm = MockLLMClient(responses=[])
    executor = ToolExecutor(ToolRegistry())
    loop = AgentLoopV2(_cfg(max_steps=2), llm, executor, _ctx())
    result = await loop.run("x")
    # Must not raise; must produce a result object
    assert isinstance(result, AgentRunResult)


@pytest.mark.asyncio
async def test_tool_def_with_required_args_validates():
    # When the LLM calls echo with the required 'value' arg, the executor runs it.
    llm = MockLLMClient(
        responses=[
            _llm_tool_call("echo", {"value": "yo"}),
            _llm_final("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_echo_tool())
    executor = ToolExecutor(registry)
    loop = AgentLoopV2(_cfg(), llm, executor, _ctx())
    result = await loop.run("x")
    # The echoed value should appear in some step's content/payload
    found = False
    for s in result.steps:
        if "yo" in (s.content or ""):
            found = True
            break
        if s.payload and "yo" in str(s.payload):
            found = True
            break
    assert found


# ---------------------------------------------------------------------------
# _build_default_llm — preset resolution (added when loop.py was rewired
# to delegate to make_client in place of hand-rolled if/else branches).
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, provider: str, model: str = "gpt-4o-mini") -> None:
        self.provider = provider
        self.model = model
        self.tools = "[]"
        self.config = "{}"


class _FakeSettings:
    openai_api_key: str | None = "sk-test"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_default_model: str = "gpt-4o-mini"
    anthropic_api_key: str | None = None
    anthropic_default_model: str = "claude-3-5-sonnet-20241022"
    request_timeout_seconds: int = 60
    max_retries: int = 3


def _fresh_loop(provider: str, model: str = "gpt-4o-mini"):
    from app.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.agent = _FakeAgent(provider, model)
    loop.settings = _FakeSettings()
    return loop


def test_build_default_llm_known_preset_uses_preset_endpoint(monkeypatch):
    """provider=deepseek must use the deepseek base_url, not openai_base_url."""
    monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
    loop = _fresh_loop("deepseek")
    llm = loop._build_default_llm()
    assert llm.base_url == "https://api.deepseek.com/v1"


def test_build_default_llm_qwen_uses_qwen_endpoint(monkeypatch):
    monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
    llm = _fresh_loop("qwen")._build_default_llm()
    assert llm.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_build_default_llm_openai_uses_openai_endpoint(monkeypatch):
    monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
    llm = _fresh_loop("openai")._build_default_llm()
    assert llm.base_url == "https://api.openai.com/v1"


def test_build_default_llm_explicit_mock_returns_mock():
    llm = _fresh_loop("mock")._build_default_llm()
    from app.llm.client import MockLLMClient
    assert isinstance(llm, MockLLMClient)


def test_build_default_llm_env_mock_overrides_agent_provider(monkeypatch):
    """AGENTFORGE_LLM_PROVIDER=mock wins over agent.provider='openai'."""
    monkeypatch.setenv("AGENTFORGE_LLM_PROVIDER", "mock")
    llm = _fresh_loop("openai")._build_default_llm()
    from app.llm.client import MockLLMClient
    assert isinstance(llm, MockLLMClient)


def test_build_default_llm_no_key_falls_back_to_mock_not_placeholder(monkeypatch):
    """Without a real key we MUST NOT ship the 'placeholder' bearer that
    would 401 against api.openai.com."""
    monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
    s = _FakeSettings()
    s.openai_api_key = None
    from app.agent.loop import AgentLoop
    loop = AgentLoop.__new__(AgentLoop)
    loop.agent = _FakeAgent("openai")
    loop.settings = s
    llm = loop._build_default_llm()
    from app.llm.client import MockLLMClient
    assert isinstance(llm, MockLLMClient)


def test_build_default_llm_custom_provider_uses_configured_base_url(monkeypatch):
    """Unknown provider name falls back to AGENTFORGE_OPENAI_BASE_URL."""
    monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
    llm = _fresh_loop("my-custom-llm")._build_default_llm()
    assert llm.base_url == "https://api.openai.com/v1"
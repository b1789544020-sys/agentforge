"""Tests for the Anthropic Messages API client.

Covers schema translation, HTTP behaviour via MockTransport, error mapping,
streaming, and the make_client / AgentLoop integration.
"""
import asyncio
import json
import os
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import httpx
from app.llm.client import (
    AnthropicClient, LLMAuthError, LLMRateLimitError, LLMServerError,
    LLMError, LLMClient,
    _split_system_from_messages, _convert_tools_to_anthropic,
    _convert_messages_to_anthropic, _convert_anthropic_to_chat_result,
    _map_anthropic_error, assemble_streamed_tool_calls, make_client,
)

# ---------------------------------------------------------------------------
# Schema translation
# ---------------------------------------------------------------------------


def test_split_system_from_messages_lifts_system():
    messages = [
        {"role": "system", "content": "You are Claude."},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "extra rule."},
        {"role": "assistant", "content": "hello"},
    ]
    system, remaining = _split_system_from_messages(messages)
    assert system == "You are Claude.\n\nextra rule."
    assert [m["role"] for m in remaining] == ["user", "assistant"]


def test_split_system_from_messages_no_system():
    messages = [{"role": "user", "content": "hi"}]
    system, remaining = _split_system_from_messages(messages)
    assert system is None
    assert remaining == messages


def test_convert_tools_to_anthropic_strips_function_wrapper():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        }
    ]
    out = _convert_tools_to_anthropic(tools)
    assert out == [
        {
            "name": "get_weather",
            "description": "Get the weather.",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            },
        }
    ]


def test_convert_tools_to_anthropic_none_when_empty():
    assert _convert_tools_to_anthropic(None) is None
    assert _convert_tools_to_anthropic([]) is None


def test_convert_messages_to_anthropic_wraps_user_text():
    out = _convert_messages_to_anthropic(
        [{"role": "user", "content": "hello"}]
    )
    assert out == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    ]


def test_convert_messages_to_anthropic_assistant_tool_use():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": json.dumps({"city": "Tokyo"}),
                    },
                }
            ],
        }
    ]
    out = _convert_messages_to_anthropic(messages)
    assert out == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_abc",
                    "name": "get_weather",
                    "input": {"city": "Tokyo"},
                }
            ],
        }
    ]


def test_convert_messages_to_anthropic_coalesces_tool_results():
    messages = [
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": json.dumps({"city": "Tokyo"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json.dumps({"temp": 20}),
        },
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "content": json.dumps({"temp": 18}),
        },
        {"role": "assistant", "content": "It is around 20 C."},
    ]
    out = _convert_messages_to_anthropic(messages)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant"]
    assert out[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": json.dumps({"temp": 20}),
        },
        {
            "type": "tool_result",
            "tool_use_id": "call_2",
            "content": json.dumps({"temp": 18}),
        },
    ]


def test_convert_anthropic_to_chat_result_text_only():
    data = {
        "id": "msg_1",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "hi back"}],
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }
    result = _convert_anthropic_to_chat_result(data)
    assert result.content == "hi back"
    assert result.tool_calls == []
    assert result.finish_reason == "end_turn"
    assert result.usage == {"total_tokens": 10, "prompt_tokens": 7, "completion_tokens": 3}


def test_convert_anthropic_to_chat_result_with_tool_use():
    data = {
        "id": "msg_2",
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "let me check"},
            {
                "type": "tool_use",
                "id": "toolu_42",
                "name": "get_weather",
                "input": {"city": "Paris"},
            },
        ],
        "usage": {"input_tokens": 11, "output_tokens": 9},
    }
    result = _convert_anthropic_to_chat_result(data)
    assert result.content == "let me check"
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "toolu_42"
    assert call.name == "get_weather"
    assert call.arguments == {"city": "Paris"}


def test_map_anthropic_error_auth_rate_server_other():
    assert isinstance(_map_anthropic_error(401, "x"), LLMAuthError)
    assert isinstance(_map_anthropic_error(403, "x"), LLMAuthError)
    assert isinstance(_map_anthropic_error(429, "x"), LLMRateLimitError)
    assert isinstance(_map_anthropic_error(500, "x"), LLMServerError)
    assert isinstance(_map_anthropic_error(400, "bad"), LLMError)


# ---------------------------------------------------------------------------
# HTTP behaviour (httpx.MockTransport)
# ---------------------------------------------------------------------------


def _install_mock_transport(client, handler):
    captured = []
    def _async_handler(request):
        captured.append(request)
        return handler(request)
    transport = httpx.MockTransport(_async_handler)
    client._client = httpx.AsyncClient(
        transport=transport,
        timeout=client.timeout,
        headers={
            "x-api-key": client.api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    return captured


def test_anthropic_chat_hits_messages_endpoint_and_converts_response():
    def handler(request):
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "claude-3-5-sonnet-20241022"
        assert body["max_tokens"] == 1024
        assert body["system"] == "Be brief."
        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        ]
        assert request.headers["x-api-key"] == "sk-ant-test"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in {k.lower() for k in request.headers.keys()}
        assert request.url.path.endswith("/v1/messages")
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "id": "msg_1",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "hello there"}],
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                }
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
            request=request,
        )
    client = AnthropicClient(api_key="sk-ant-test", max_retries=0)
    captured = _install_mock_transport(client, handler)
    try:
        result = asyncio.run(
            client.chat(
                messages=[
                    {"role": "system", "content": "Be brief."},
                    {"role": "user", "content": "hi"},
                ]
            )
        )
    finally:
        asyncio.run(client.close())
    assert len(captured) == 1
    assert result.content == "hello there"
    assert result.tool_calls == []
    assert result.finish_reason == "end_turn"
    assert result.prompt_tokens == 5
    assert result.completion_tokens == 2


def test_anthropic_chat_sends_tools_in_input_schema_shape():
    def handler(request):
        body = json.loads(request.content.decode("utf-8"))
        assert body["tools"] == [
            {
                "name": "sum",
                "description": "Add two numbers.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "number"},
                        "b": {"type": "number"},
                    },
                },
            }
        ]
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "id": "msg_t",
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_t",
                            "name": "sum",
                            "input": {"a": 1, "b": 2},
                        }
                    ],
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                }
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
            request=request,
        )
    client = AnthropicClient(api_key="sk-ant-test", max_retries=0)
    _install_mock_transport(client, handler)
    try:
        result = asyncio.run(
            client.chat(
                messages=[{"role": "user", "content": "1+2?"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "sum",
                            "description": "Add two numbers.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "a": {"type": "number"},
                                    "b": {"type": "number"},
                                },
                            },
                        },
                    }
                ],
            )
        )
    finally:
        asyncio.run(client.close())
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.name == "sum"
    assert call.arguments == {"a": 1, "b": 2}


def test_anthropic_chat_raises_auth_error_on_401():
    def handler(request):
        return httpx.Response(
            401,
            content=b'{"type":"error","error":{"type":"authentication_error"}}',
            headers={"content-type": "application/json"},
            request=request,
        )
    client = AnthropicClient(api_key="sk-bad", max_retries=0)
    _install_mock_transport(client, handler)
    try:
        try:
            asyncio.run(
                client.chat(messages=[{"role": "user", "content": "hi"}])
            )
        except LLMAuthError as exc:
            assert "anthropic auth failed (401)" in str(exc)
        else:
            raise AssertionError("expected LLMAuthError")
    finally:
        asyncio.run(client.close())


def test_anthropic_chat_retries_5xx_then_succeeds():
    attempts = {"n": 0}
    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                503,
                content=b'{"type":"error","error":{"type":"overloaded_error"}}',
                headers={"content-type": "application/json"},
                request=request,
            )
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "id": "msg_ok",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "fine"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
            request=request,
        )
    client = AnthropicClient(
        api_key="sk-ant-test", max_retries=2, backoff_base=0.0
    )
    _install_mock_transport(client, handler)
    try:
        result = asyncio.run(
            client.chat(messages=[{"role": "user", "content": "hi"}])
        )
    finally:
        asyncio.run(client.close())
    assert attempts["n"] == 2
    assert result.content == "fine"


def test_anthropic_streaming_yields_text_and_done():
    sse_lines = [
        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_s","usage":{"input_tokens":3}}}\n',
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}\n',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n',
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n',
    ]
    sse_body = "\n\n".join(sse_lines) + "\n\n"

    async def _run():
        async def handler(request):
            return httpx.Response(
                200,
                content=sse_body.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        client = AnthropicClient(api_key="sk-ant-test", max_retries=0)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=client.timeout,
            headers={"x-api-key": "sk-ant-test"},
        )
        try:
            chunks = []
            async for c in client.chat_stream(
                messages=[{"role": "user", "content": "hi"}]
            ):
                chunks.append(c)
            return chunks
        finally:
            await client.close()

    chunks = asyncio.run(_run())
    text = "".join(c.content_delta for c in chunks if c.content_delta)
    assert text == "Hello"
    final = chunks[-1]
    assert final.done is True
    assert final.finish_reason == "end_turn"
    assert final.completion_tokens == 2


def test_anthropic_streaming_assembles_tool_calls():
    sse_lines = [
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_t","name":"sum","input":{}}}\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"a\\":"}}\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"1,\\"b\\":2}"}}\n',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n',
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":1}}\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n',
    ]
    sse_body = "\n\n".join(sse_lines) + "\n\n"

    async def _run():
        async def handler(request):
            return httpx.Response(
                200,
                content=sse_body.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        client = AnthropicClient(api_key="sk-ant-test", max_retries=0)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=client.timeout,
            headers={"x-api-key": "sk-ant-test"},
        )
        try:
            chunks = []
            async for c in client.chat_stream(
                messages=[{"role": "user", "content": "1+2?"}]
            ):
                chunks.append(c)
        finally:
            await client.close()
        return chunks

    chunks = asyncio.run(_run())
    tool_calls = assemble_streamed_tool_calls(chunks)
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "toolu_t"
    assert tool_calls[0].name == "sum"
    assert tool_calls[0].arguments == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# make_client dispatch
# ---------------------------------------------------------------------------


def test_make_client_routes_anthropic_to_anthropic_client():
    c = make_client("anthropic", "sk-ant-test")
    assert isinstance(c, AnthropicClient)
    assert c.base_url == "https://api.anthropic.com"
    assert c.model == "claude-3-5-sonnet-20241022"
    asyncio.run(c.close())


def test_make_client_other_providers_stay_on_llm_client():
    for name in ("deepseek", "qwen", "openai"):
        c = make_client(name, "sk-test")
        assert isinstance(c, LLMClient), name
        assert not isinstance(c, AnthropicClient), name
        asyncio.run(c.close())


# ---------------------------------------------------------------------------
# AgentLoop integration
# ---------------------------------------------------------------------------


def test_loop_build_default_llm_uses_anthropic_key_and_preset(monkeypatch):
    """provider=anthropic must (a) read anthropic_api_key (not openai_api_key)
    and (b) return an AnthropicClient with the Anthropic base URL."""
    monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
    from app.agent.loop import AgentLoop
    from app.config import get_settings
    import types
    cfg = get_settings()
    cfg.openai_api_key = "sk-openai-leak"
    cfg.anthropic_api_key = "sk-ant-real"
    cfg.anthropic_default_model = "claude-3-5-haiku-20241022"
    fake_agent = types.SimpleNamespace(
        provider="anthropic", model="", system_prompt=""
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop.agent = fake_agent
    loop.settings = cfg
    llm = loop._build_default_llm()
    assert isinstance(llm, AnthropicClient)
    assert llm.api_key == "sk-ant-real"
    assert llm.model == "claude-3-5-haiku-20241022"
    assert llm.base_url == "https://api.anthropic.com"


def test_loop_anthropic_provider_without_key_falls_back_to_mock(monkeypatch):
    monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
    from app.agent.loop import AgentLoop
    from app.config import get_settings
    from app.llm.client import MockLLMClient
    import types
    cfg = get_settings()
    cfg.openai_api_key = None
    cfg.anthropic_api_key = None
    fake_agent = types.SimpleNamespace(
        provider="anthropic", model="", system_prompt=""
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop.agent = fake_agent
    loop.settings = cfg
    llm = loop._build_default_llm()
    assert isinstance(llm, MockLLMClient)

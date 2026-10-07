"""Smoke test for the LLM client.  Runs without any real network calls."""

import asyncio
import json
import sys
import os

# Make ``app`` importable when running this file directly.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import httpx  # noqa: E402

from app.llm.client import (  # noqa: E402
    LLMClient,
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    ToolCall,
    assemble_streamed_tool_calls,
    make_client,
    StreamChunk,
)


def _build_response(status_code: int, body, headers=None):
    """Construct a synthetic httpx.Response."""
    if isinstance(body, (dict, list)):
        content = json.dumps(body).encode("utf-8")
    else:
        content = (body or "").encode("utf-8")
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers=headers or {"content-type": "application/json"},
        request=httpx.Request("POST", "http://example/chat/completions"),
    )


async def test_make_client_presets():
    for name in ("deepseek", "qwen", "openai"):
        c = make_client(name, "test-key")
        assert c.base_url.startswith("https://"), c.base_url
        assert c.model, c.model
        await c.close()
    # unknown provider without base_url
    try:
        make_client("bogus", "k")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    # unknown with base_url
    c = make_client("bogus", "k", base_url="https://x/v1")
    await c.close()
    print("OK make_client_presets")


async def test_chat_non_stream(monkeypatch=False):
    client = LLMClient(
        api_key="k",
        base_url="https://example.com/v1",
        model="test-model",
        max_retries=0,
    )
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello there",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": json.dumps({"city": "Beijing"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }
    resp = _build_response(200, payload)

    async def fake_post(url, json=None, **kw):  # noqa: A002
        return resp

    client._client.post = fake_post  # type: ignore[assignment]
    result = await client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )
    assert result.content == "Hello there"
    assert result.token_count == 20
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 8
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.name == "get_weather"
    assert tc.arguments == {"city": "Beijing"}
    assert tc.id == "call_1"
    await client.close()
    print("OK chat_non_stream")


async def test_auth_error_no_retry():
    client = LLMClient(
        api_key="k",
        base_url="https://example.com/v1",
        model="m",
        max_retries=3,
    )
    resp = _build_response(401, {"error": {"message": "bad key"}})

    async def fake_post(url, json=None, **kw):
        return resp

    client._client.post = fake_post  # type: ignore[assignment]
    raised = False
    try:
        await client.chat(messages=[{"role": "user", "content": "hi"}])
    except LLMAuthError as e:
        raised = True
        assert "401" in str(e)
    assert raised, "expected LLMAuthError"
    await client.close()
    print("OK auth_error_no_retry")


async def test_rate_limit_retries():
    """429 should be retried up to max_retries, then raised."""
    client = LLMClient(
        api_key="k",
        base_url="https://example.com/v1",
        model="m",
        max_retries=2,
        backoff_base=0.01,
        backoff_cap=0.02,
    )
    calls = {"n": 0}

    async def fake_post(url, json=None, **kw):
        calls["n"] += 1
        return _build_response(429, {"error": "rate limited"})

    client._client.post = fake_post  # type: ignore[assignment]
    raised = False
    try:
        await client.chat(messages=[{"role": "user", "content": "hi"}])
    except LLMRateLimitError:
        raised = True
    assert raised
    # 1 initial + 2 retries = 3 total
    assert calls["n"] == 3, calls["n"]
    await client.close()
    print("OK rate_limit_retries")


async def test_server_error_retries_then_success():
    client = LLMClient(
        api_key="k",
        base_url="https://example.com/v1",
        model="m",
        max_retries=3,
        backoff_base=0.01,
        backoff_cap=0.02,
    )
    calls = {"n": 0}
    ok = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    async def fake_post(url, json=None, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return _build_response(503, {"error": "down"})
        return _build_response(200, ok)

    client._client.post = fake_post  # type: ignore[assignment]
    result = await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert result.content == "ok"
    assert calls["n"] == 3
    await client.close()
    print("OK server_error_retry_then_success")


async def test_timeout_error():
    client = LLMClient(
        api_key="k",
        base_url="https://example.com/v1",
        model="m",
        max_retries=1,
        backoff_base=0.01,
    )

    async def fake_post(url, json=None, **kw):
        raise httpx.ConnectTimeout("simulated")

    client._client.post = fake_post  # type: ignore[assignment]
    raised = False
    try:
        await client.chat(messages=[{"role": "user", "content": "hi"}])
    except LLMTimeoutError:
        raised = True
    assert raised
    await client.close()
    print("OK timeout_error")


async def test_streaming():
    """Stream a fake SSE response and verify chunks are decoded."""
    client = LLMClient(
        api_key="k",
        base_url="https://example.com/v1",
        model="m",
        max_retries=0,
    )
    sse_lines = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":" world"},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}',
        "data: [DONE]",
    ]
    body = ("\n".join(sse_lines) + "\n").encode("utf-8")

    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    response = httpx.Response(
        200,
        content=body,
        headers={"content-type": "text/event-stream"},
        request=request,
    )

    def fake_stream(method, url, json=None, **kw):
        class _CM:
            async def __aenter__(self_inner):
                return response

            async def __aexit__(self_inner, *exc):
                return False

        return _CM()

    client._client.stream = fake_stream  # type: ignore[assignment]
    chunks = []
    async for c in client.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        chunks.append(c)
    text = "".join(c.content_delta for c in chunks if c.content_delta)
    assert text == "Hello world", repr(text)
    # final chunk carries usage
    done_chunks = [c for c in chunks if c.done]
    assert done_chunks, "missing done chunk"
    assert done_chunks[-1].token_count == 5
    assert done_chunks[-1].prompt_tokens == 3
    assert done_chunks[-1].completion_tokens == 2
    await client.close()
    print("OK streaming")


async def test_streaming_tool_calls():
    """Tool-call deltas should be assemblable into a complete ToolCall."""
    client = LLMClient(
        api_key="k",
        base_url="https://example.com/v1",
        model="m",
        max_retries=0,
    )
    sse_lines = [
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_42","function":{"name":"search","arguments":"{\\"q\\":"}}]}}]}',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"hi\\"}"}}]}}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    body = ("\n".join(sse_lines) + "\n").encode("utf-8")
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    response = httpx.Response(
        200, content=body,
        headers={"content-type": "text/event-stream"},
        request=request,
    )

    def fake_stream(method, url, json=None, **kw):
        class _CM:
            async def __aenter__(self_inner):
                return response

            async def __aexit__(self_inner, *exc):
                return False

        return _CM()

    client._client.stream = fake_stream  # type: ignore[assignment]
    chunks: list[StreamChunk] = []
    async for c in client.chat_stream(messages=[{"role": "user", "content": "x"}]):
        chunks.append(c)
    assembled = assemble_streamed_tool_calls(chunks)
    assert len(assembled) == 1
    assert assembled[0].id == "call_42"
    assert assembled[0].name == "search"
    assert assembled[0].arguments == {"q": "hi"}
    await client.close()
    print("OK streaming_tool_calls")


async def main():
    await test_make_client_presets()
    await test_chat_non_stream()
    await test_auth_error_no_retry()
    await test_rate_limit_retries()
    await test_server_error_retries_then_success()
    await test_timeout_error()
    await test_streaming()
    await test_streaming_tool_calls()
    print("\nALL LLM CLIENT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
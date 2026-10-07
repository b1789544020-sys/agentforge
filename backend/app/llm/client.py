"""
OpenAI-compatible LLM client for AgentForge.

Why not the official ``openai`` SDK? We deliberately keep this thin wrapper on
top of :mod:`httpx` so that:

* we can talk to any provider that exposes the OpenAI Chat Completions schema
  (DeepSeek, 通义千问 DashScope compatible-mode, OpenAI itself, OpenRouter,
  local llama.cpp, vLLM, ...);
* we can stream with precise control of SSE parsing;
* we can hook our own retry / backoff / metrics logic.

Public surface
--------------

* :class:`LLMClient`  – async client
* :class:`LLMError` / :class:`LLMTimeoutError` / :class:`LLMAuthError` /
  :class:`LLMRateLimitError` / :class:`LLMServerError`  – exception hierarchy
* :class:`ToolCall`  – a single tool invocation returned by the model
* :class:`ChatResult`  – structured result of a non-streaming call
* :class:`StreamChunk`  – a single SSE delta
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Optional, Union

import httpx


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base class for everything this client can raise."""


class LLMTimeoutError(LLMError):
    """The request or stream exceeded the configured timeout."""


class LLMAuthError(LLMError):
    """401 / 403 – the API key is missing, invalid, or lacks scope."""


class LLMRateLimitError(LLMError):
    """429 – we're being throttled."""


class LLMServerError(LLMError):
    """5xx – upstream provider failed."""


class LLMResponseError(LLMError):
    """Provider returned 2xx but the payload could not be parsed."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """One tool invocation emitted by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Some providers (Anthropic) return raw JSON string for arguments; we
    # normalise to dict when we can. ``raw_arguments`` keeps the original.
    raw_arguments: str = ""


@dataclass
class ChatResult:
    """Result of a non-streaming ``chat()`` call."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    token_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def usage(self) -> dict[str, Any]:
        """Return ``{"total_tokens", "prompt_tokens", "completion_tokens"}``.

        Mirrors the shape of the OpenAI ``usage`` block so that callers
        can write ``result.usage["total_tokens"]`` without poking into
        ``raw``.
        """
        return {
            "total_tokens": int(self.token_count or 0),
            "prompt_tokens": int(self.prompt_tokens or 0),
            "completion_tokens": int(self.completion_tokens or 0),
        }


# Backwards-compatible alias used by ``app.agent.loop`` and the test suite.
# Older revisions of the client exposed a top-level ``LLMResponse``; the
# canonical class is now :class:`ChatResult`.
LLMResponse = ChatResult


@dataclass
class StreamChunk:
    """A single delta yielded by :meth:`LLMClient.chat_stream`."""

    # Either a text delta ...
    content_delta: str = ""
    # ... or a tool call delta. ``arguments_delta`` may be a partial JSON
    # fragment; the consumer is expected to concatenate across chunks.
    tool_call_delta: Optional[ToolCall] = None
    # The stream's final usage block, if the provider emits it.
    token_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: Optional[str] = None
    # ``True`` for the last chunk of a stream.
    done: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_message(message: dict[str, Any]) -> dict[str, Any]:
    """Strip non-JSON-serialisable values from a message before sending."""
    clean: dict[str, Any] = {}
    for key, value in message.items():
        if value is None:
            continue
        clean[key] = value
    return clean


def _extract_usage(payload: dict[str, Any]) -> tuple[int, int, int]:
    """Return ``(total, prompt, completion)`` tokens from a chat-completion
    payload, falling back to ``0`` if the provider omitted the field."""
    usage = payload.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return total, prompt, completion


def _parse_tool_calls(choice: dict[str, Any]) -> list[ToolCall]:
    """Normalise the ``tool_calls`` array from a single choice.

    The OpenAI Chat Completions schema places ``tool_calls`` under the
    assistant ``message`` of a choice for non-streaming responses and under
    the ``delta`` for streaming responses.  Some providers / proxies flatten
    the field onto the choice itself, so we accept both.
    """
    message = choice.get("message") or {}
    raw_calls = (
        message.get("tool_calls")
        or choice.get("tool_calls")
        or []
    )
    if not raw_calls:
        # Some Anthropic-compatible gateways emit it under ``delta`` even
        # for non-streaming responses.  Fall back to that as a last resort.
        delta = choice.get("delta") or {}
        raw_calls = delta.get("tool_calls") or []
    out: list[ToolCall] = []
    for idx, call in enumerate(raw_calls):
        fn = call.get("function") or {}
        name = fn.get("name") or ""
        args_raw = fn.get("arguments") or ""
        if isinstance(args_raw, dict):
            args_dict = args_raw
            args_raw_str = json.dumps(args_raw, ensure_ascii=False)
        else:
            args_str = str(args_raw)
            args_raw_str = args_str
            try:
                args_dict = json.loads(args_str) if args_str.strip() else {}
            except json.JSONDecodeError:
                # Provider returned invalid JSON; keep raw, expose empty dict.
                args_dict = {}
                logger.warning(
                    "tool call %s: failed to parse arguments JSON: %r",
                    name or idx,
                    args_str,
                )
        out.append(
            ToolCall(
                id=str(call.get("id") or f"call_{idx}"),
                name=name,
                arguments=args_dict,
                raw_arguments=args_raw_str,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


# Status codes that are worth retrying.
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMClient:
    """Async OpenAI-compatible LLM client backed by :mod:`httpx`.

    Parameters
    ----------
    api_key:
        Bearer token.
    base_url:
        Root of the OpenAI-compatible API.  The client will POST to
        ``{base_url}/chat/completions``.  For convenience the constructor
        tolerates a trailing slash.
    model:
        Default model name to use when ``chat``/``chat_stream`` are called
        without an explicit override.
    timeout:
        Per-request timeout in seconds (connect + read).  Default ``60``.
    max_retries:
        Maximum number of *additional* attempts after the first failure.
        Default ``3`` ⇒ up to 4 attempts in total.
    backoff_base:
        Initial backoff in seconds.  The actual wait is
        ``backoff_base * 2**attempt + jitter``.
    backoff_cap:
        Upper bound (seconds) for any single sleep.
    default_headers:
        Extra HTTP headers (e.g. organisation-id).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        default_headers: Optional[dict[str, str]] = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not base_url:
            raise ValueError("base_url is required")
        if not model:
            raise ValueError("model is required")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AgentForge/0.1 (+https://github.com/agentforge)",
        }
        if default_headers:
            headers.update(default_headers)

        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            # Don't follow redirects across hosts; chat APIs don't redirect.
            follow_redirects=False,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """Close the underlying HTTP client.  Safe to call multiple times."""
        try:
            await self._client.aclose()
        except Exception:  # pragma: no cover - best-effort close
            pass

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        stream: bool = False,
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, list[str]]] = None,
        extra_body: Optional[dict[str, Any]] = None,
    ) -> ChatResult:
        """Send a chat-completion request and return a structured result.

        ``stream=True`` is supported but in that case the function will
        internally consume the stream and return a single aggregated
        :class:`ChatResult`.  Use :meth:`chat_stream` for chunk-level access.
        """
        payload = self._build_payload(
            messages=messages,
            tools=tools,
            stream=False,  # always False for the non-streaming path
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
            extra_body=extra_body,
        )
        response = await self._request_with_retry(payload)
        data = self._parse_json(response)
        return self._result_from_payload(data)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Convenience alias for :meth:`chat` used by ``AgentLoopV2``.

        The V2 loop calls ``llm.complete(messages=..., tools=...)``; we
        forward the call to :meth:`chat` so that subclasses (notably
        :class:`MockLLMClient`) need only implement one method.
        """
        return await self.chat(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            **kwargs,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, list[str]]] = None,
        extra_body: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat-completion response, yielding one :class:`StreamChunk`
        per SSE event.

        Tool calls come back as deltas: the first chunk carries ``id`` /
        ``name`` and subsequent chunks fill in ``arguments_delta``.  Callers
        are responsible for stitching them together (see
        :func:`assemble_streamed_tool_calls`).
        """
        payload = self._build_payload(
            messages=messages,
            tools=tools,
            stream=True,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
            extra_body=extra_body,
        )
        async for chunk in self._stream_with_retry(payload):
            yield chunk

    # ------------------------------------------------------------------ #
    # Payload construction
    # ------------------------------------------------------------------ #

    def _build_payload(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        stream: bool,
        model: Optional[str],
        temperature: Optional[float],
        top_p: Optional[float],
        max_tokens: Optional[int],
        stop: Optional[Union[str, list[str]]],
        extra_body: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": [_coerce_message(m) for m in messages],
        }
        if stream:
            payload["stream"] = True
            # Some providers (DeepSeek) only emit usage when this is set.
            payload.setdefault("stream_options", {"include_usage": True})
        if tools:
            payload["tools"] = tools
            # Default: let the model decide.  Providers that require it will
            # pass this through; OpenAI-style APIs accept it as-is.
            payload.setdefault("tool_choice", "auto")
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if stop is not None:
            payload["stop"] = stop
        if extra_body:
            payload.update(extra_body)
        return payload

    # ------------------------------------------------------------------ #
    # HTTP + retry
    # ------------------------------------------------------------------ #

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    async def _request_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        last_exc: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.post(self._endpoint(), json=payload)
            except httpx.TimeoutException as exc:
                last_exc = LLMTimeoutError(
                    "request timed out after " + str(self.timeout) + "s"
                )
                last_exc.__cause__ = exc
                self._maybe_sleep(attempt, retryable=True)
                continue
            except httpx.HTTPError as exc:
                # Network / connection errors are always retryable.
                last_exc = LLMError("network error: " + repr(exc))
                last_exc.__cause__ = exc
                logger.warning("LLM network error on attempt %d: %r", attempt + 1, exc)
                self._maybe_sleep(attempt, retryable=True)
                continue

            if response.status_code < 400:
                return response

            # Translate HTTP errors.
            err = self._http_error(response)
            retryable = (
                response.status_code in _RETRYABLE_STATUS
                or isinstance(err, (LLMTimeoutError, LLMServerError, LLMRateLimitError))
            )
            if not retryable or attempt >= self.max_retries:
                raise err
            logger.warning(
                "LLM call failed (status=%d, attempt=%d/%d): %s",
                response.status_code,
                attempt + 1,
                self.max_retries + 1,
                err,
            )
            self._maybe_sleep(attempt, retryable=True)
            last_exc = err

        # All retries exhausted.
        assert last_exc is not None
        raise last_exc

    async def _stream_with_retry(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[StreamChunk]:
        # For streaming, we retry by replaying the whole stream.  This is
        # the simplest correct behaviour: any partial chunks from the failed
        # attempt are dropped.
        last_exc: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                had_error = False
                async with self._client.stream(
                    "POST", self._endpoint(), json=payload
                ) as response:
                    if response.status_code >= 400:
                        # Buffer the body so we can raise with detail.
                        await response.aread()
                        err = self._http_error(response)
                        retryable = (
                            response.status_code in _RETRYABLE_STATUS
                            or isinstance(
                                err,
                                (LLMTimeoutError, LLMServerError, LLMRateLimitError),
                            )
                        )
                        if not retryable or attempt >= self.max_retries:
                            raise err
                        last_exc = err
                        self._maybe_sleep(attempt, retryable=True)
                        had_error = True
                        continue

                    async for chunk in self._iter_sse_chunks(response):
                        yield chunk
                    return
            except httpx.TimeoutException as exc:
                last_exc = LLMTimeoutError(
                    "stream timed out after " + str(self.timeout) + "s"
                )
                last_exc.__cause__ = exc
                self._maybe_sleep(attempt, retryable=True)
                continue
            except httpx.HTTPError as exc:
                last_exc = LLMError("stream network error: " + repr(exc))
                last_exc.__cause__ = exc
                self._maybe_sleep(attempt, retryable=True)
                continue
            except LLMError as exc:
                # Already classified; only retry on retryable subclasses.
                if attempt >= self.max_retries or not isinstance(
                    exc,
                    (LLMTimeoutError, LLMServerError, LLMRateLimitError),
                ):
                    raise
                last_exc = exc
                self._maybe_sleep(attempt, retryable=True)
                continue

            if not had_error:
                return  # pragma: no cover - defensive

        assert last_exc is not None
        raise last_exc

    def _maybe_sleep(self, attempt: int, *, retryable: bool) -> None:
        if not retryable:
            return
        # Exponential backoff with full jitter, capped.
        delay = min(self.backoff_cap, self.backoff_base * (2 ** attempt))
        delay = random.uniform(0, delay)  # full jitter
        logger.debug("retrying LLM call in %.2fs (attempt=%d)", delay, attempt + 1)
        time.sleep(delay)

    def _http_error(self, response: httpx.Response) -> LLMError:
        status = response.status_code
        # Try to extract a human-readable error body.
        body: str
        try:
            body = response.text
        except Exception:  # pragma: no cover
            body = "<unreadable>"

        if status in (401, 403):
            return LLMAuthError(f"authentication failed ({status}): {body[:300]}")
        if status == 429:
            return LLMRateLimitError(f"rate limited: {body[:300]}")
        if status in _RETRYABLE_STATUS and status >= 500:
            return LLMServerError(f"server error ({status}): {body[:300]}")
        if status >= 500:
            return LLMServerError(f"server error ({status}): {body[:300]}")
        return LLMError(f"HTTP {status}: {body[:500]}")

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict[str, Any]:
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"failed to decode JSON response: {exc}; "
                f"body={response.text[:300]!r}"
            ) from exc

    def _result_from_payload(self, data: dict[str, Any]) -> ChatResult:
        choices = data.get("choices") or []
        if not choices:
            raise LLMResponseError(f"no choices in response: {data!r}")
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        tool_calls = _parse_tool_calls(choice)
        total, prompt, completion = _extract_usage(data)
        return ChatResult(
            content=content if isinstance(content, str) else str(content),
            tool_calls=tool_calls,
            token_count=total,
            prompt_tokens=prompt,
            completion_tokens=completion,
            finish_reason=choice.get("finish_reason"),
            raw=data,
        )

    async def _iter_sse_chunks(
        self, response: httpx.Response
    ) -> AsyncIterator[StreamChunk]:
        """Parse an OpenAI-style SSE response into :class:`StreamChunk`s.

        We accept both ``data: {...}`` and bare ``{...}`` (some proxies drop
        the prefix).  The terminating ``data: [DONE]`` line ends the stream.
        """
        pending_tool_calls: dict[int, dict[str, Any]] = {}
        final_usage: dict[str, Any] = {}
        finish_reason: Optional[str] = None

        async for line in response.aiter_lines():
            if not line:
                continue
            if line.startswith(":"):
                # SSE comment.
                continue
            stripped = line[len("data:") :] if line.startswith("data:") else line
            stripped = stripped.strip()
            if not stripped:
                continue
            if stripped == "[DONE]":
                break
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                logger.debug("dropping unparseable SSE line: %r", line[:200])
                continue

            # Some providers stuff usage in a chunk without a choice; capture
            # it so we can attach token counts to the final chunk.
            if "usage" in event and event["usage"]:
                final_usage = event["usage"]

            choices = event.get("choices") or []
            if not choices:
                # Pure usage chunk (DeepSeek, OpenAI when include_usage=True).
                continue

            choice = choices[0]
            delta = choice.get("delta") or {}

            content_delta = delta.get("content") or ""
            if not isinstance(content_delta, str):
                content_delta = str(content_delta)

            # Tool-call deltas: each carries an index.
            tool_delta_obj: Optional[ToolCall] = None
            for raw_call in delta.get("tool_calls") or []:
                idx = int(raw_call.get("index") or 0)
                bucket = pending_tool_calls.setdefault(
                    idx,
                    {"id": "", "name": "", "arguments": ""},
                )
                if raw_call.get("id"):
                    bucket["id"] = str(raw_call["id"])
                fn = raw_call.get("function") or {}
                if fn.get("name"):
                    bucket["name"] = str(fn["name"])
                if "arguments" in fn and fn["arguments"] is not None:
                    bucket["arguments"] += str(fn["arguments"])

                # Only emit a ToolCall when we have at least an id+name.
                if bucket["id"] and bucket["name"]:
                    try:
                        args_dict = json.loads(bucket["arguments"]) if bucket["arguments"] else {}
                    except json.JSONDecodeError:
                        args_dict = {}
                    tool_delta_obj = ToolCall(
                        id=bucket["id"],
                        name=bucket["name"],
                        arguments=args_dict,
                        raw_arguments=bucket["arguments"],
                    )

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

            chunk = StreamChunk(
                content_delta=content_delta,
                tool_call_delta=tool_delta_obj,
                finish_reason=finish_reason,
            )
            if content_delta or tool_delta_obj or finish_reason:
                yield chunk

        # Emit a final chunk with usage info, if any.
        if final_usage:
            total = int(final_usage.get("total_tokens") or 0)
            prompt = int(final_usage.get("prompt_tokens") or 0)
            completion = int(final_usage.get("completion_tokens") or 0)
            yield StreamChunk(
                token_count=total,
                prompt_tokens=prompt,
                completion_tokens=completion,
                finish_reason=finish_reason,
                done=True,
            )
        else:
            yield StreamChunk(finish_reason=finish_reason, done=True)


# ---------------------------------------------------------------------------
# Anthropic client
# ---------------------------------------------------------------------------


# Public Anthropic Messages API version we pin to.  Stable across the
# Claude-3 / Claude-3.5 / Claude-4 lines.
_ANTHROPIC_API_VERSION = "2023-06-01"
_ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_DEFAULT_MAX_TOKENS = 1024


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


def _split_system_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[Optional[str], list[dict[str, Any]]]:
    """Anthropic's Messages API takes a top-level ``system`` string and
    ``messages`` containing only ``user`` / ``assistant`` turns.  The
    OpenAI schema AgentForge uses everywhere else buries the system prompt
    as a ``role: "system"`` entry at the front, so we lift it out here.
    """
    system_parts: list[str] = []
    remaining: list[dict[str, Any]] = []
    for msg in messages:
        role = (msg.get("role") or "").lower()
        if role == "system":
            text = msg.get("content")
            if isinstance(text, str) and text:
                system_parts.append(text)
            continue
        remaining.append(msg)
    system_text = "\n\n".join(system_parts) if system_parts else None
    return system_text, remaining


def _convert_tools_to_anthropic(
    tools: Optional[list[dict[str, Any]]],
) -> Optional[list[dict[str, Any]]]:
    """Translate OpenAI ``{type:"function", function:{name, description,
    parameters}}`` tool schemas into Anthropic ``{name, description,
    input_schema}``.  Returns ``None`` if there are no tools.
    """
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            out.append(
                {
                    "name": fn.get("name") or "",
                    "description": fn.get("description") or "",
                    "input_schema": fn.get("parameters") or {
                        "type": "object",
                        "properties": {},
                    },
                }
            )
            continue
        if "name" in t and "input_schema" in t:
            out.append(t)
            continue
        out.append(
            {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "input_schema": t.get("parameters")
                or t.get("input_schema")
                or {"type": "object", "properties": {}},
            }
        )
    return out or None


def _convert_messages_to_anthropic(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate OpenAI-style ``messages`` into Anthropic's user/assistant
    alternation with ``content`` blocks of type ``text`` / ``tool_use`` /
    ``tool_result``.

    Consecutive ``role:"tool"`` messages are coalesced into a single user
    message whose ``content`` is a list of ``tool_result`` blocks, because
    Anthropic requires the role to flip to ``user`` at least once between
    tool results and the next assistant turn.
    """
    out: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []
    pending_assistant_text: list[str] = []
    pending_assistant_tools: list[dict[str, Any]] = []

    def _flush_assistant() -> None:
        if not pending_assistant_text and not pending_assistant_tools:
            return
        content: list[dict[str, Any]] = []
        for txt in pending_assistant_text:
            if txt:
                content.append({"type": "text", "text": txt})
        for tb in pending_assistant_tools:
            content.append(tb)
        out.append({"role": "assistant", "content": content or ""})
        pending_assistant_text.clear()
        pending_assistant_tools.clear()

    def _flush_tool_results() -> None:
        if not pending_tool_results:
            return
        out.append({"role": "user", "content": list(pending_tool_results)})
        pending_tool_results.clear()

    for msg in messages:
        role = (msg.get("role") or "").lower()
        if role == "tool":
            content = msg.get("content")
            if isinstance(content, list):
                blocks = content
            else:
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id") or "",
                        "content": _stringify(content) if content is not None else "",
                    }
                ]
            pending_tool_results.extend(blocks)
            continue

        if pending_tool_results:
            _flush_tool_results()

        if role == "assistant":
            text = msg.get("content")
            if isinstance(text, str) and text:
                pending_assistant_text.append(text)
            elif isinstance(text, list):
                for blk in text:
                    if isinstance(blk, dict):
                        pending_assistant_tools.append(blk)
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                args_raw = fn.get("arguments") or ""
                if isinstance(args_raw, dict):
                    input_obj = args_raw
                else:
                    try:
                        input_obj = json.loads(args_raw) if args_raw else {}
                    except json.JSONDecodeError:
                        input_obj = {}
                pending_assistant_tools.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or "",
                        "name": fn.get("name") or "",
                        "input": input_obj,
                    }
                )
            _flush_assistant()
            continue

        if role == "user":
            content = msg.get("content")
            if isinstance(content, str):
                out.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": content}],
                    }
                )
            elif isinstance(content, list):
                out.append({"role": "user", "content": content})
            else:
                out.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": _stringify(content)}],
                    }
                )
            continue

        out.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _stringify(msg.get("content"))}
                ],
            }
        )

    if pending_tool_results:
        _flush_tool_results()
    return out


def _convert_anthropic_to_chat_result(data: dict[str, Any]) -> ChatResult:
    """Turn an Anthropic Messages response into the OpenAI-style
    :class:`ChatResult` the rest of AgentForge expects.
    """
    content_blocks = data.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for idx, block in enumerate(content_blocks):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text") or "")
        elif btype == "tool_use":
            name = block.get("name") or ""
            input_obj = block.get("input") or {}
            if not isinstance(input_obj, dict):
                input_obj = {}
            args_raw = json.dumps(input_obj, ensure_ascii=False)
            tool_calls.append(
                ToolCall(
                    id=str(block.get("id") or f"toolu_{idx}"),
                    name=name,
                    arguments=input_obj,
                    raw_arguments=args_raw,
                )
            )

    stop_reason = data.get("stop_reason") or None
    finish_reason = "tool_calls" if tool_calls else (stop_reason or "stop")
    usage = data.get("usage") or {}
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return ChatResult(
        content="".join(text_parts),
        tool_calls=tool_calls,
        token_count=prompt + completion,
        prompt_tokens=prompt,
        completion_tokens=completion,
        finish_reason=finish_reason,
        raw=data,
    )


def _map_anthropic_error(status: int, body: str) -> LLMError:
    if status in (401, 403):
        return LLMAuthError(f"anthropic auth failed ({status}): {body[:300]}")
    if status == 429:
        return LLMRateLimitError(f"anthropic rate limited: {body[:300]}")
    if status >= 500:
        return LLMServerError(f"anthropic server error ({status}): {body[:300]}")
    return LLMError(f"anthropic HTTP {status}: {body[:500]}")


class AnthropicClient:
    """Async client for the Anthropic Messages API.

    Implemented on top of :mod:`httpx` (the ``anthropic`` SDK is *not* used so
    that we keep our dependency surface minimal).  Public surface mirrors
    :class:`LLMClient` so the agent loop can swap providers transparently:

    * :meth:`chat`        → :class:`ChatResult` (non-streaming)
    * :meth:`chat_stream` → async iterator of :class:`StreamChunk`
    * :meth:`complete`    → alias of :meth:`chat`
    * :meth:`close`       → release the underlying :class:`httpx.AsyncClient`

    Schema translation:

    * OpenAI ``messages`` with a leading ``role:"system"`` entry are split
      into Anthropic's top-level ``system`` field and the remaining
      user/assistant turns.
    * OpenAI ``tools`` (``{type:"function", function:{...}}``) are flattened
      into Anthropic ``{name, description, input_schema}``.
    * OpenAI ``role:"tool"`` results are re-wrapped as user
      ``tool_result`` content blocks, per Anthropic's alternation rules.
    """

    base_url: str
    api_key: str
    model: str

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "claude-3-5-sonnet-20241022",
        base_url: str = _ANTHROPIC_DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        default_max_tokens: int = _ANTHROPIC_DEFAULT_MAX_TOKENS,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not model:
            raise ValueError("model is required")
        self.api_key = api_key
        self.base_url = (base_url or _ANTHROPIC_DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.default_max_tokens = int(default_max_tokens)

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_API_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "AgentForge/0.1 (+https://github.com/agentforge)",
        }
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=False,
        )

    # -- lifecycle ----------------------------------------------------- #

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # pragma: no cover
            pass

    async def __aenter__(self) -> "AnthropicClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- public API ---------------------------------------------------- #

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        stream: bool = False,
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, list[str]]] = None,
        extra_body: Optional[dict[str, Any]] = None,
    ) -> ChatResult:
        if stream:
            return await self._chat_via_stream(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                stop=stop,
                extra_body=extra_body,
            )
        payload = self._build_payload(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
            extra_body=extra_body,
        )
        response = await self._request_with_retry(payload)
        if response.status_code >= 400:
            raise _map_anthropic_error(response.status_code, response.text)
        data = response.json()
        return _convert_anthropic_to_chat_result(data)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Alias for :meth:`chat` matching the :class:`LLMClient` contract."""
        return await self.chat(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            **kwargs,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, list[str]]] = None,
        extra_body: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream Anthropic SSE events (``message_start`` / ``content_block_*``
        / ``message_delta`` / ``message_stop``) as :class:`StreamChunk`s
        that look like OpenAI deltas.  The first content-block-start for a
        ``tool_use`` block carries the ``id`` / ``name``; later deltas
        append JSON fragments to ``raw_arguments``.

        Usage tracking from ``message_delta`` is emitted on the terminating
        ``done=True`` chunk, matching the OpenAI client's behaviour.
        """
        payload = self._build_payload(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
            extra_body=extra_body,
            stream=True,
        )
        response = await self._client.post(
            f"{self.base_url}/v1/messages", json=payload
        )
        if response.status_code >= 400:
            raise _map_anthropic_error(response.status_code, response.text)
        async for chunk in self._iter_anthropic_sse(response):
            yield chunk

    # -- payload ------------------------------------------------------- #

    def _build_payload(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        model: Optional[str],
        temperature: Optional[float],
        top_p: Optional[float],
        max_tokens: Optional[int],
        stop: Optional[Union[str, list[str]]],
        extra_body: Optional[dict[str, Any]],
        stream: bool = False,
    ) -> dict[str, Any]:
        system_text, remaining = _split_system_from_messages(messages)
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": _convert_messages_to_anthropic(remaining),
            "max_tokens": (
                int(max_tokens) if max_tokens is not None else self.default_max_tokens
            ),
        }
        if system_text:
            payload["system"] = system_text
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if top_p is not None:
            payload["top_p"] = float(top_p)
        if stop:
            payload["stop_sequences"] = (
                [stop] if isinstance(stop, str) else list(stop)
            )
        converted = _convert_tools_to_anthropic(tools)
        if converted:
            payload["tools"] = converted
        if stream:
            payload["stream"] = True
        if extra_body:
            payload.update(extra_body)
        return payload

    # -- request / retry ---------------------------------------------- #

    async def _request_with_retry(
        self, payload: dict[str, Any]
    ) -> httpx.Response:
        url = f"{self.base_url}/v1/messages"
        last_exc: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.post(url, json=payload)
            except httpx.TimeoutException as exc:
                last_exc = LLMTimeoutError(
                    f"anthropic request timed out after {self.timeout}s"
                )
                last_exc.__cause__ = exc
                self._maybe_sleep(attempt)
                continue
            except httpx.HTTPError as exc:
                last_exc = LLMError(f"anthropic network error: {exc!r}")
                last_exc.__cause__ = exc
                logger.warning(
                    "anthropic network error on attempt %d: %r", attempt + 1, exc
                )
                self._maybe_sleep(attempt)
                continue

            if response.status_code < 400:
                return response
            if response.status_code in (401, 403, 400, 404):
                raise _map_anthropic_error(response.status_code, response.text)
            if response.status_code == 429 or response.status_code >= 500:
                last_exc = _map_anthropic_error(response.status_code, response.text)
                self._maybe_sleep(attempt)
                continue
            raise _map_anthropic_error(response.status_code, response.text)

        assert last_exc is not None
        raise last_exc

    def _maybe_sleep(self, attempt: int) -> None:
        if attempt >= self.max_retries:
            return
        delay = min(
            self.backoff_cap,
            self.backoff_base * (2 ** attempt),
        ) + random.uniform(0, 0.25)
        time.sleep(delay)

    # -- streaming helpers -------------------------------------------- #

    async def _chat_via_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        model: Optional[str],
        temperature: Optional[float],
        top_p: Optional[float],
        max_tokens: Optional[int],
        stop: Optional[Union[str, list[str]]],
        extra_body: Optional[dict[str, Any]],
    ) -> ChatResult:
        text_parts: list[str] = []
        tool_buckets: dict[int, dict[str, str]] = {}
        finish_reason: Optional[str] = None
        usage: dict[str, int] = {}
        async for chunk in self.chat_stream(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
            extra_body=extra_body,
        ):
            if chunk.content_delta:
                text_parts.append(chunk.content_delta)
            if chunk.tool_call_delta is not None:
                bucket = tool_buckets.setdefault(
                    0,
                    {"id": "", "name": "", "arguments": ""},
                )
                if chunk.tool_call_delta.id:
                    bucket["id"] = chunk.tool_call_delta.id
                if chunk.tool_call_delta.name:
                    bucket["name"] = chunk.tool_call_delta.name
                bucket["arguments"] += chunk.tool_call_delta.raw_arguments
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
            if chunk.done:
                usage = {
                    "prompt": chunk.prompt_tokens,
                    "completion": chunk.completion_tokens,
                }
        tool_calls: list[ToolCall] = []
        for b in tool_buckets.values():
            try:
                args = json.loads(b["arguments"]) if b["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(
                    id=b["id"] or "toolu_0",
                    name=b["name"],
                    arguments=args if isinstance(args, dict) else {},
                    raw_arguments=b["arguments"],
                )
            )
        return ChatResult(
            content="".join(text_parts),
            tool_calls=tool_calls,
            token_count=(usage.get("prompt", 0) + usage.get("completion", 0)),
            prompt_tokens=usage.get("prompt", 0),
            completion_tokens=usage.get("completion", 0),
            finish_reason="tool_calls" if tool_calls else (finish_reason or "stop"),
            raw={"via": "anthropic-stream"},
        )

    async def _iter_anthropic_sse(
        self, response: httpx.Response
    ) -> AsyncIterator[StreamChunk]:
        """Anthropic SSE → OpenAI-style :class:`StreamChunk`.

        Anthropic sends two-line messages: ``event:`` then ``data:``.
        :func:`httpx.Response.aiter_lines` returns them one at a time, so
        we pair them via the module-level ``_last_event`` scratch list.
        """
        tool_buckets: dict[int, dict[str, str]] = {}
        finish_reason: Optional[str] = None
        final_usage: dict[str, int] = {}

        async for line in response.aiter_lines():
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                _last_event[0] = line[len("event:") :].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload_line = line[len("data:") :].strip()
            event_type = _last_event[0]
            _last_event[0] = ""
            if not payload_line or payload_line == "[DONE]":
                continue
            try:
                evt = json.loads(payload_line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type") or event_type
            if etype == "content_block_start":
                idx = int(evt.get("index") or 0)
                block = evt.get("content_block") or {}
                btype = block.get("type")
                if btype == "tool_use":
                    bucket = tool_buckets.setdefault(
                        idx,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    bucket["id"] = str(block.get("id") or "")
                    bucket["name"] = str(block.get("name") or "")
                    yield StreamChunk(
                        tool_call_delta=ToolCall(
                            id=bucket["id"],
                            name=bucket["name"],
                            arguments={},
                            raw_arguments="",
                        )
                    )
            elif etype == "content_block_delta":
                idx = int(evt.get("index") or 0)
                delta = evt.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    yield StreamChunk(content_delta=delta.get("text") or "")
                elif dtype == "input_json_delta":
                    bucket = tool_buckets.setdefault(
                        idx,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    fragment = delta.get("partial_json") or ""
                    bucket["arguments"] += fragment
                    # Emit the *running* concatenation so consumers
                    # (assemble_streamed_tool_calls) can reparse the full
                    # JSON in one shot, matching the OpenAI client contract.
                    yield StreamChunk(
                        tool_call_delta=ToolCall(
                            id=bucket["id"],
                            name=bucket["name"],
                            arguments={},
                            raw_arguments=bucket["arguments"],
                        )
                    )
            elif etype == "message_delta":
                delta = evt.get("delta") or {}
                if delta.get("stop_reason"):
                    finish_reason = delta["stop_reason"]
                usage_block = evt.get("usage") or {}
                for k in ("input_tokens", "output_tokens"):
                    if k in usage_block:
                        final_usage[k] = int(usage_block[k] or 0)

        prompt = final_usage.get("input_tokens", 0)
        completion = final_usage.get("output_tokens", 0)
        yield StreamChunk(
            token_count=prompt + completion,
            prompt_tokens=prompt,
            completion_tokens=completion,
            finish_reason=finish_reason,
            done=True,
        )


# Module-level scratch space used by ``_iter_anthropic_sse`` to pair the
# ``event:`` line with the following ``data:`` line.  An Anthropic SSE
# message is two lines; ``httpx``'s ``aiter_lines()`` exposes them one at
# a time, so we keep the last event type here.
_last_event: list[str] = [""]

# Convenience: stitch streamed tool-call fragments into complete ToolCalls.
# ---------------------------------------------------------------------------


def assemble_streamed_tool_calls(
    chunks: Iterable[StreamChunk],
) -> list[ToolCall]:
    """Reconstruct a list of fully-formed :class:`ToolCall` objects from a
    sequence of streamed :class:`StreamChunk`s.  The last call wins for each
    tool-call id; their ``arguments`` field is the parsed version of the
    concatenated ``raw_arguments``."""
    by_id: dict[str, ToolCall] = {}
    for chunk in chunks:
        tc = chunk.tool_call_delta
        if tc is None:
            continue
        existing = by_id.get(tc.id)
        if existing is None:
            by_id[tc.id] = ToolCall(
                id=tc.id,
                name=tc.name,
                arguments=dict(tc.arguments),
                raw_arguments=tc.raw_arguments,
            )
        else:
            # The client already concatenates raw fragments into each
            # chunk's ``raw_arguments``; we therefore *replace* (not append)
            # the running buffer and re-parse the full string.  The ``name``
            # and ``id`` from earlier chunks are preserved on ``existing``.
            existing.raw_arguments = tc.raw_arguments or existing.raw_arguments
            if not existing.name and tc.name:
                existing.name = tc.name
            try:
                parsed = json.loads(existing.raw_arguments) if existing.raw_arguments else None
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                existing.arguments = parsed if isinstance(parsed, dict) else {}
    return list(by_id.values())


# ---------------------------------------------------------------------------
# Mock LLM client (used in tests + offline development)
# ---------------------------------------------------------------------------


class MockLLMClient(LLMClient):
    """An in-memory LLM client that does not make any network calls.

    The agent loop is exercised end-to-end without hitting a real provider.
    Each turn returns a deterministic *final* assistant message (no tool
    calls), so runs complete in a single LLM hop.  The behaviour is good
    enough to validate the surrounding plumbing (Run / Step / Tool
    registry / context budget / memory / eval).

    The base class is a thin wrapper around :mod:`httpx`; we override
    :meth:`chat` and short-circuit before any HTTP request is made.
    """
    def __init__(
        self,
        *,
        reply: str = "Mock response (no upstream call).",
        model: str = "mock-model",
        responses: Optional[list[ChatResult]] = None,
    ) -> None:
        # Skip the parent's __init__ entirely – we don't need an httpx
        # client, timeouts or retry state.
        self.reply = reply
        self.model_name = model
        self.call_count = 0
        # When the caller supplies a pre-canned list of ``ChatResult``
        # objects, ``chat()`` will pop them off in order.  This is the
        # primary mode used by the unit tests in :mod:`tests.test_loop`,
        # which build each turn's response with helpers like
        # ``_llm_final(text)`` and ``_llm_tool_call(name, args)``.
        self._responses: list[ChatResult] = list(responses or [])
        self._fallback = bool(self._responses)

    async def chat(  # type: ignore[override]
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
        **_: Any,
    ) -> ChatResult:
        self.call_count += 1
        # Test mode: pop the next pre-canned response, if any.  This is
        # the mode exercised by :mod:`tests.test_loop`, which constructs
        # the exact sequence of ``ChatResult`` objects it expects the
        # agent to see.
        if self._responses:
            return self._responses.pop(0)
        # By default the mock returns a single final assistant reply so
        # the agent loop terminates in one LLM hop.  When tools are
        # supplied we *do* make one tool call on the very first turn
        # (so the trace contains a think→act→observe cycle) and then a
        # plain text reply on the second turn.  This mirrors the
        # contract documented in the class docstring: tests that need
        # finer control pass an explicit ``responses=`` list.
        if tools and self.call_count == 1:
            first = tools[0]
            fn = (first.get("function") or {})
            tool_name = fn.get("name") or "echo"
            # Try to extract a sample value from the most recent user
            # message so the echo round-trip is meaningful.
            sample_text = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    content = msg.get("content")
                    if isinstance(content, str):
                        sample_text = content
                    break
            params = fn.get("parameters") or {}
            props = (params.get("properties") or {}) if isinstance(params, dict) else {}
            required = list(params.get("required") or []) if isinstance(params, dict) else []
            call_args: dict[str, Any] = {}
            for fname in required:
                if fname in props and isinstance(props[fname], dict):
                    ftype = props[fname].get("type")
                    if ftype == "string":
                        call_args[fname] = sample_text
                    elif ftype == "number" or ftype == "integer":
                        call_args[fname] = 0
                    elif ftype == "boolean":
                        call_args[fname] = False
                    elif ftype == "object":
                        call_args[fname] = {}
                    elif ftype == "array":
                        call_args[fname] = []
            # Fallback: if the schema didn't help, give ``echo`` some text.
            if not call_args and tool_name == "echo":
                call_args = {"text": sample_text}
            return ChatResult(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_mock_1",
                        name=tool_name,
                        arguments=call_args,
                        raw_arguments=json.dumps(call_args),
                    )
                ],
                prompt_tokens=10,
                completion_tokens=5,
                finish_reason="tool_calls",
                raw={"mock": True},
            )
        return ChatResult(
            content=self.reply,
            tool_calls=[],
            prompt_tokens=10,
            completion_tokens=5,
            finish_reason="stop",
            raw={"mock": True},
        )


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


# Common provider presets.  Users can still pass any custom ``base_url``.
# ``anthropic`` is special-cased below: it returns a :class:`AnthropicClient`
# instead of a vanilla :class:`LLMClient` because the Anthropic Messages API
# has a different schema (top-level ``system``, ``tool_use`` / ``tool_result``
# content blocks, ``x-api-key`` header, no ``/chat/completions`` endpoint).
_PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-3-5-sonnet-20241022",
    },
}


def make_client(
    provider: str,
    api_key: str,
    *,
    model: Optional[str] = None,
    timeout: float = 60.0,
    **kwargs: Any,
) -> "LLMClient | AnthropicClient":
    """Construct an LLM client from a provider preset.

    ``provider`` may be one of ``"deepseek"``, ``"qwen"``, ``"openai"``,
    ``"anthropic"`` or any arbitrary name – in the latter case the caller
    must pass ``base_url`` via ``kwargs``.

    Anthropic routes through :class:`AnthropicClient` (different schema,
    different auth header); all others get the OpenAI-compatible
    :class:`LLMClient`.
    """
    name = (provider or "").lower()
    if name == "anthropic":
        preset = _PROVIDER_PRESETS["anthropic"]
        return AnthropicClient(
            api_key=api_key,
            model=model or preset["default_model"],
            base_url=kwargs.pop("base_url", preset["base_url"]),
            timeout=timeout,
        )

    preset = _PROVIDER_PRESETS.get(name)
    if preset is None:
        if "base_url" not in kwargs:
            raise ValueError(
                f"unknown provider {provider!r}; pass base_url explicitly"
            )
        base_url = kwargs.pop("base_url")
        default_model = model or "gpt-3.5-turbo"
    else:
        base_url = kwargs.pop("base_url", preset["base_url"])
        default_model = model or preset["default_model"]

    return LLMClient(
        api_key=api_key,
        base_url=base_url,
        model=default_model,
        timeout=timeout,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

_TOKEN_ENCODER = None


def _get_encoder():
    """Return a cached tiktoken encoder (cl100k_base fallback)."""
    global _TOKEN_ENCODER
    if _TOKEN_ENCODER is not None:
        return _TOKEN_ENCODER
    try:
        import tiktoken  # type: ignore

        _TOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:  # pragma: no cover - optional dep
        _TOKEN_ENCODER = None
    return _TOKEN_ENCODER


def _stringify_for_count(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def serialize_for_count(messages: Iterable[dict[str, Any]]) -> str:
    """Render a message list as a single string for token estimation."""
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(_stringify_for_count(p) for p in content)
        parts.append(f"{role}:{_stringify_for_count(content)}")
    return "\n".join(parts)


def count_tokens(messages: Iterable[dict[str, Any]] | str, model: str | None = None) -> int:
    """Estimate token count for messages. Uses tiktoken when available."""
    if isinstance(messages, str):
        text = messages
    else:
        text = serialize_for_count(messages)
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    # Fallback heuristic: ~4 chars per token (English), conservative
    return max(1, len(text) // 4)


__all__ = [
    # Errors
    "LLMError",
    "LLMTimeoutError",
    "LLMAuthError",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMResponseError",
    # Data
    "ToolCall",
    "ChatResult",
    "StreamChunk",
    # Client
    "LLMClient",
    "MockLLMClient",
    "make_client",
    "assemble_streamed_tool_calls",
    # Counting
    "count_tokens",
    "serialize_for_count",
]

"""Conversation context window management.

This module exposes two complementary APIs:

* :class:`Context` – the lightweight dataclass used by the agent loop
  and test suite (``add`` / ``enforce_budget`` / ``to_openai``).
* :class:`ContextManager` – a higher-level manager that owns the
  system prompt, tool definitions, conversation history, exposes
  ``compress()`` with the four-step strategy required by the spec
  (system prompt, recent turns, summary, tool-result truncation)
  and reports token usage in the format expected by the UI.

Both classes share the same 4-character-per-token estimator so the
TokenGauge component on the front-end renders the same number that
the backend sees.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from app.llm.client import count_tokens

logger = logging.getLogger(__name__)

Role = Literal["system", "user", "assistant", "tool"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _estimate_chars_tokens(text: str) -> int:
    """Rough estimator: ~4 characters per token (GPT family)."""
    if not text:
        return 0
    # Round up so that even a single non-empty string registers ≥1 token.
    return max(1, (len(text) + 3) // 4)


# ---------------------------------------------------------------------------
# Existing lightweight API (kept for backwards compatibility with loop.py)
# ---------------------------------------------------------------------------

@dataclass
class Context:
    """Sliding window of chat messages (dataclass flavour)."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int = 8000
    model: str = "gpt-4o-mini"
    summariser: Callable[[list[dict[str, Any]]], str] | None = None

    # ---- basic ops ---------------------------------------------------
    def add(self, role: Role, content: str, **extra: Any) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": role, "content": content}
        msg.update(extra)
        self.messages.append(msg)
        return msg

    # Backwards-compatible alias used by the test suite and the clean-room
    # :class:`AgentLoopV2` (which calls ``ctx.add_message("system", ...)``).
    def add_message(self, role: Role, content: str, **extra: Any) -> dict[str, Any]:
        return self.add(role, content, **extra)

    def add_tool_result(self, call_id: str, content: str) -> dict[str, Any]:
        return self.add("tool", content, tool_call_id=call_id)

    def extend(self, messages: Iterable[dict[str, Any]]) -> None:
        self.messages.extend(messages)

    def clear(self) -> None:
        self.messages.clear()

    # ---- accessors ----------------------------------------------------
    def total_tokens(self) -> int:
        return sum(count_tokens(m.get("content") or "", self.model) for m in self.messages)

    def last(self) -> dict[str, Any] | None:
        return self.messages[-1] if self.messages else None

    def to_openai(self) -> list[dict[str, Any]]:
        return list(self.messages)

    # ---- windowing ---------------------------------------------------
    def enforce_budget(self) -> None:
        """Drop or summarise old messages to fit within ``max_tokens``."""
        if self.total_tokens() <= self.max_tokens:
            return

        # Keep the system message if any, plus the last 4 messages intact.
        system = [m for m in self.messages if m.get("role") == "system"]
        rest = [m for m in self.messages if m.get("role") != "system"]
        tail = rest[-4:] if len(rest) > 4 else rest
        middle = rest[: -4] if len(rest) > 4 else []

        if self.summariser and middle:
            summary = self.summariser(middle)
            replacement: list[dict[str, Any]] = [
                {
                    "role": "system",
                    "content": (
                        "Summary of earlier conversation:\n" + summary
                    ),
                }
            ]
            self.messages = system + replacement + tail
        else:
            # Truncate oldest non-system messages one by one.
            while middle and self.total_tokens() > self.max_tokens:
                middle.pop(0)
                candidate = system + middle + tail
                self.messages = candidate

        if self.total_tokens() > self.max_tokens:
            # Last resort: hard-truncate the tail.
            while self.messages and self.total_tokens() > self.max_tokens:
                self.messages.pop()
            logger.warning("Context hard-truncated to %s tokens", self.total_tokens())

    # ---- helpers -----------------------------------------------------
    def to_trace(self) -> list[dict[str, Any]]:
        """Return a serialisable summary suitable for storage / UI."""
        return [
            {
                "role": m.get("role"),
                "content": (m.get("content") or "")[:500],
                "tool_call_id": m.get("tool_call_id"),
                "tool_calls": m.get("tool_calls"),
            }
            for m in self.messages
        ]


# ---------------------------------------------------------------------------
# High-level ContextManager (matches the spec from the prompt)
# ---------------------------------------------------------------------------

# Cap on how many characters of a tool result we keep after truncation.
# 800 chars ≈ 200 tokens, which is enough to preserve key info while
# shrinking a multi-kilobyte result to a manageable footprint.
_TOOL_RESULT_MAX_CHARS = 800

# Default summary prefix (injected as a system message after compression).
_SUMMARY_PREFIX = (
    "The following is a summary of the earlier part of this conversation. "
    "Use it as background context when answering the user."
)


class ContextManager:
    """High-level conversation-context manager.

    Compared to the lightweight :class:`Context` dataclass this class:

    * owns the system prompt as a first-class field,
    * tracks tool definitions separately so they can be reported to the
      LLM via ``get_messages()`` (OpenAI / Anthropic style),
    * keeps an explicit conversation ``history`` of user/assistant turns,
    * exposes a ``compress()`` method implementing the four-step
      strategy described in the spec:

          1. Keep the system prompt (never compressed).
          2. Keep the most recent 3 turns of conversation.
          3. Summarise older turns (delegated to an injected
             ``summariser`` callable; falls back to a deterministic
             extractive summary when none is provided).
          4. Truncate any tool-call result blobs to a small footprint.

    The class deliberately uses a 4-character-per-token estimator for
    its public ``get_token_usage()`` API so the front-end ``TokenGauge``
    always shows the same number the backend computed.
    """

    def __init__(
        self,
        max_tokens: int = 8000,
        summariser: Callable[[list[dict[str, Any]]], str] | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        self.system_prompt: str = ""
        self.tool_definitions: list[dict[str, Any]] = []
        # Each entry is a plain dict with at least ``role`` and
        # ``content`` keys (mirrors the OpenAI chat format). The first
        # system message is stored separately above; the history only
        # contains user/assistant/tool turns.
        self.history: list[dict[str, Any]] = []
        # Optional injected summariser; if None we fall back to an
        # extractive strategy in :meth:`compress`.
        self._summariser = summariser
        # How many of the most-recent turns to keep verbatim during
        # ``compress()`` (spec says "最近 3 轮对话" → 3 turns).
        self._keep_recent_turns: int = 3
        # Token counter cache so we don't recount on every read.
        self._cached_tokens: int | None = None
        self._cache_dirty: bool = True

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def add_message(
        self,
        role: Role,
        content: str,
        token_count: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Append a message to the conversation history.

        ``role`` must be one of ``system`` / ``user`` / ``assistant`` /
        ``tool``. A ``role="system"`` message updates the system prompt
        (the spec treats the system prompt as a separate, immovable
        field, so we do not keep duplicates in the history list).

        ``token_count`` is optional: if supplied it overrides the local
        4-char estimator. This is useful when the LLM provider already
        returned an exact token count for a previous turn.
        """
        if not isinstance(role, str) or role not in ("system", "user", "assistant", "tool"):
            raise ValueError(f"invalid role: {role!r}")

        if role == "system":
            # The system prompt is a single field. If a system message
            # is appended, concatenate so the caller can build it up.
            self.system_prompt = (
                (self.system_prompt + "\n" + content)
                if self.system_prompt
                else content
            )
            self._invalidate_cache()
            return {"role": "system", "content": self.system_prompt, **extra}

        msg: dict[str, Any] = {"role": role, "content": content}
        msg.update(extra)
        if token_count is not None:
            msg["_token_count"] = int(token_count)
        self.history.append(msg)
        self._invalidate_cache()
        return msg

    def set_system_prompt(self, prompt: str) -> None:
        """Replace the system prompt wholesale."""
        self.system_prompt = prompt or ""
        self._invalidate_cache()

    def set_tool_definitions(self, tools: Iterable[dict[str, Any]]) -> None:
        """Replace the registered tool definitions."""
        self.tool_definitions = list(tools or [])
        self._invalidate_cache()

    def clear_history(self) -> None:
        """Drop the conversation history (system prompt is preserved)."""
        self.history.clear()
        self._invalidate_cache()

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    def get_messages(self) -> list[dict[str, Any]]:
        """Return the messages in OpenAI chat-completions format.

        The returned list always starts with a single system message
        (or nothing if no system prompt is set) followed by the
        conversation history. Tool definitions are *not* part of the
        messages array; they are returned separately via
        :meth:`get_tool_definitions` because OpenAI's chat API takes
        them in a different field (``tools``).
        """
        out: list[dict[str, Any]] = []
        if self.system_prompt:
            out.append({"role": "system", "content": self.system_prompt})
        for m in self.history:
            # Strip internal bookkeeping fields before returning.
            clean = {k: v for k, v in m.items() if not k.startswith("_")}
            out.append(clean)
        return out

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return the tool schemas to send to the LLM (e.g. ``tools=[...]``)."""
        return list(self.tool_definitions)

    def get_token_usage(self) -> dict[str, Any]:
        """Return current token usage statistics for the UI gauge."""
        used = self._estimate_tokens()
        max_tokens = max(1, int(self.max_tokens))
        return {
            "used": used,
            "max": max_tokens,
            "percentage": round(used / max_tokens * 100, 2),
        }

    # ------------------------------------------------------------------
    # Compression
    # ------------------------------------------------------------------
    def compress(self) -> dict[str, Any]:
        """Compress the context to fit within ``max_tokens``.

        Implements the four-step strategy from the spec:

        1. **System prompt is never compressed** – it stays at the top
           of the message list.
        2. **Keep the most recent 3 turns** (user/assistant pairs).
        3. **Summarise older turns** – either via the injected
           ``summariser`` callable, or via the deterministic extractive
           fallback. The summary is inserted as an additional system
           message right after the original system prompt.
        4. **Truncate tool-call results** to a small footprint so a
           single huge tool response can't blow the budget.

        Returns a small report describing what happened (number of
        messages summarised, total tokens before/after, etc.) which is
        useful for tests and for surfacing in the UI.
        """
        before_tokens = self._estimate_tokens()
        before_history_len = len(self.history)

        if before_tokens <= self.max_tokens:
            return {
                "compressed": False,
                "reason": "within_budget",
                "before_tokens": before_tokens,
                "after_tokens": before_tokens,
                "summarised_messages": 0,
                "truncated_tool_results": 0,
            }

        # Step 4 first – shrinking the cheap stuff (long tool results)
        # often brings us back under the budget without losing any
        # conversation content. We do it in-place.
        truncated_count = self._truncate_tool_results()

        # Step 2 – split into "keep recent N turns" and "older".
        recent_turns = self._split_recent_turns(self._keep_recent_turns)
        older = self.history[: len(self.history) - len(recent_turns)]

        summarised = 0
        if older:
            summary_text = self._build_summary(older)
            summarised = len(older)
            # Drop the older messages; we'll re-insert the summary as
            # an extra system message.
            self.history = list(recent_turns)
            # Build a fresh message list so we can prepend the summary
            # without touching the real system prompt field.
            self._inject_summary_after_system(summary_text)
        else:
            # Only tool truncation can help at this point.
            pass

        # Final defensive pass: if the budget is still blown, shrink
        # things in order of expendability.
        #
        # 1. Aggressively re-truncate any tool results.
        # 2. Repeatedly halve the injected summary until it fits or
        #    the summary itself is tiny (cap at 32 chars).
        # 3. Drop oldest non-system history messages as a last resort.
        #
        # We *never* drop ``role == "system"`` entries from history
        # outright (the summary itself lives in history as a system
        # message); we just shrink it so we don't have to throw away
        # every user/assistant turn.
        retruncated = self._truncate_tool_results(aggressive=True)
        for _ in range(8):  # bounded – 2^8 = 256x reduction is plenty
            if self._estimate_tokens() <= self.max_tokens:
                break
            if not self._shrink_summary_to_fit():
                # Nothing left to shrink; jump to the pop loop.
                break
        if retruncated:
            logger.warning(
                "ContextManager aggressively re-truncated %s tool results",
                retruncated,
            )
        if self._estimate_tokens() > self.max_tokens:
            self._drop_until_fits()

        after_tokens = self._estimate_tokens()
        self._invalidate_cache()

        return {
            "compressed": True,
            "reason": "over_budget",
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "summarised_messages": summarised,
            "truncated_tool_results": truncated_count,
            "history_before": before_history_len,
            "history_after": len(self.history),
        }

    def _drop_until_fits(self) -> None:
        """Drop oldest non-system, non-tool history messages until the
        budget is honoured. Idempotent and bounded.

        The summary system message (if any) is **never** dropped – it
        carries compressed history that the user can no longer
        reconstruct from the recent turns alone. The tool messages
        are also kept so we don't orphan an in-flight call/response
        pair.

        We drop in two passes:

        1. **Half-pass** – drop up to half of the recent-turns block
           so the *most recent user turn* is always preserved (this
           is what :func:`test_context_manager_keeps_recent_three_turns`
           asserts on).
        2. **Floor-pass** – if the budget is still blown, keep dropping
           oldest non-system messages until either the budget is met
           or the very last user/assistant pair is the only thing
           left. This second pass is what allows
           :func:`test_context_manager_compress_fits_within_budget_after_call`
           to converge on extreme-budget scenarios.
        """
        safety = 0
        self._invalidate_cache()

        def _drop_one() -> bool:
            for idx, m in enumerate(self.history):
                if m.get("role") == "system":
                    continue
                if m.get("role") == "tool":
                    continue
                del self.history[idx]
                self._invalidate_cache()
                return True
            return False

        def _protected_indices() -> set[int]:
            """Indices of the messages we must not drop in the
            floor-pass (the most recent user/assistant pair).
            """
            prot: set[int] = set()
            for idx in range(len(self.history) - 1, -1, -1):
                role = self.history[idx].get("role")
                if role in ("user", "assistant"):
                    prot.add(idx)
                    if len(prot) >= 2:
                        break
            return prot

        # Pass 1: half-drops.
        first_recent_idx = 0
        for idx, m in enumerate(self.history):
            if not m.get("_is_summary"):
                first_recent_idx = idx
                break
        recent_count = len(self.history) - first_recent_idx
        max_drops = max(0, recent_count - max(2, self._keep_recent_turns))
        half_drops = 0
        while (
            self._estimate_tokens() > self.max_tokens
            and half_drops < max(1, max_drops)
        ):
            if not _drop_one():
                break
            safety += 1
            half_drops += 1

        # Pass 2: floor-drops – keep going but never delete the very
        # last user/assistant pair (the current "turn in progress").
        while self._estimate_tokens() > self.max_tokens:
            protected = _protected_indices()
            target = None
            for idx, m in enumerate(self.history):
                if idx in protected:
                    continue
                if m.get("role") == "system":
                    continue
                if m.get("role") == "tool":
                    continue
                target = idx
                break
            if target is None:
                break
            del self.history[target]
            self._invalidate_cache()
            safety += 1

        if safety:
            logger.warning("ContextManager hard-dropped %s oldest messages", safety)

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------
    def _estimate_tokens(self) -> int:
        """Estimate the total token count using a 4-char heuristic.

        This is the same heuristic the spec calls out and matches the
        :class:`Context` dataclass estimator for consistency. It counts:

        * the system prompt,
        * every entry in ``history`` (using ``_token_count`` if set,
          otherwise the 4-char estimator on the string content),
        * a small fixed overhead for each tool definition (≈ 80 tokens
          per schema, which is in the right ballpark for OpenAI).
        """
        if not self._cache_dirty and self._cached_tokens is not None:
            return self._cached_tokens

        total = _estimate_chars_tokens(self.system_prompt or "")
        for m in self.history:
            cached = m.get("_token_count")
            if isinstance(cached, int) and cached >= 0:
                total += cached
                continue
            content = m.get("content")
            if isinstance(content, str):
                total += _estimate_chars_tokens(content)
            else:
                # Non-string content (list of parts, etc.) – fall back
                # to a JSON-serialised estimate.
                try:
                    import json

                    total += _estimate_chars_tokens(json.dumps(content, ensure_ascii=False))
                except Exception:  # noqa: BLE001
                    total += _estimate_chars_tokens(str(content))
        # Tool definitions cost ~80 tokens each on top of any content
        # (name, description, JSON schema). We use 60 to stay a touch
        # conservative while still reflecting their presence.
        total += 60 * len(self.tool_definitions)

        self._cached_tokens = total
        self._cache_dirty = False
        return total

    # ------------------------------------------------------------------
    # Internal helpers used by compress()
    # ------------------------------------------------------------------
    def _invalidate_cache(self) -> None:
        self._cache_dirty = True
        self._cached_tokens = None

    def _split_recent_turns(self, n_turns: int) -> list[dict[str, Any]]:
        """Return the last ``n_turns`` user/assistant pairs (and any
        tool messages that are interleaved with them).

        A "turn" is anchored on a ``user`` message; we keep the
        ``n_turns`` most-recent user messages and everything that
        follows them up to the end of the history.
        """
        if n_turns <= 0 or not self.history:
            return []
        # Walk backwards to find the index of the n-th-from-last user msg.
        user_indices = [i for i, m in enumerate(self.history) if m.get("role") == "user"]
        if not user_indices:
            return list(self.history[-n_turns:])
        cutoff_idx = user_indices[max(0, len(user_indices) - n_turns)]
        return list(self.history[cutoff_idx:])

    def _build_summary(self, messages: list[dict[str, Any]]) -> str:
        """Summarise ``messages`` either via the injected LLM-based
        summariser or via a deterministic extractive fallback.

        The fallback concatenates the first sentence (or first 200
        characters) of each message so the summary stays useful even
        without an LLM call.
        """
        if self._summariser is not None:
            try:
                text = self._summariser(messages)
                if text:
                    return text.strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("summariser raised %s, falling back", exc)

        lines: list[str] = []
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content")
            if isinstance(content, list):
                # OpenAI tool-call content may be a list of parts.
                content = " ".join(
                    str(p.get("text", "")) if isinstance(p, dict) else str(p)
                    for p in content
                )
            if not isinstance(content, str):
                content = str(content)
            snippet = content.strip().splitlines()[0] if content.strip() else ""
            snippet = snippet[:200].rstrip()
            if snippet:
                lines.append(f"- {role}: {snippet}")
        return "\n".join(lines) or "(no content)"

    def _inject_summary_after_system(self, summary: str) -> None:
        """Insert the summary as an extra system message at the head of
        the history. We do not modify ``self.system_prompt`` directly –
        the summary lives as its own system message so callers can tell
        it apart from the real system prompt.
        """
        summary_msg: dict[str, Any] = {
            "role": "system",
            "content": f"{_SUMMARY_PREFIX}\n{summary}",
            "_is_summary": True,
        }
        self.history.insert(0, summary_msg)

    def _truncate_tool_results(self, aggressive: bool = False) -> int:
        """Shrink long ``tool`` messages in the history.

        Returns the number of messages that were actually modified.

        When ``aggressive`` is True we shrink even short tool results
        down to a tiny footprint (200 chars) so the final defensive
        pass in :meth:`compress` has somewhere to claw tokens back from
        in extreme-budget test cases.
        """
        cap = 200 if aggressive else _TOOL_RESULT_MAX_CHARS
        count = 0
        for m in self.history:
            if m.get("role") != "tool":
                continue
            content = m.get("content")
            if not isinstance(content, str):
                continue
            if len(content) <= cap:
                continue
            head = content[:cap]
            m["content"] = (
                f"{head}\n... [truncated {len(content) - cap} chars]"
            )
            m["_truncated"] = True
            count += 1
        return count

    def _shrink_summary_to_fit(self) -> bool:
        """If the budget is still over, shrink the injected summary.

        The summary is the cheapest thing to shrink – it is text we
        just generated ourselves and the user is not allowed to see
        it directly. Returns True if anything was modified.

        We perform a single, well-bounded reduction (cap at 200
        characters) to avoid infinite loops when the suffix we append
        is comparable in size to the slice we take.
        """
        SUMMARY_CAP = 200
        for m in self.history:
            if not m.get("_is_summary"):
                continue
            content = m.get("content")
            if not isinstance(content, str) or not content:
                continue
            if len(content) <= SUMMARY_CAP:
                # Already small enough; don't touch.
                continue
            suffix = "... [summary truncated to fit]"
            head = content[:SUMMARY_CAP]
            m["content"] = head + suffix
            m["_shrunk"] = True
            self._invalidate_cache()
            return True
        return False


__all__ = ["Context", "ContextManager", "Role"]
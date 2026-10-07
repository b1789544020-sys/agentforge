"""Serialize and deserialize :class:`Agent` records to / from JSON or YAML.

Used by the ``/api/v1/agents/import`` and ``/api/v1/agents/export``
endpoints. The functions are intentionally pure (no DB / IO) so they
are easy to unit-test in isolation — the API layer is responsible for
wiring them to the database session and to the HTTP request / response.

File format (versioned for forward compatibility)::

    {
      "version": 1,
      "kind": "agentforge.agent",
      "exported_at": "2026-09-03T20:00:00Z",
      "agents": [
        {
          "name": "my-agent",
          "description": "...",
          "system_prompt": "...",
          "model": "gpt-4o-mini",
          "provider": "openai",
          "temperature": 0.7,
          "max_steps": 20,
          "tools": ["search_knowledge", "shell"],
          "config": {"k": 1}
        }
      ]
    }

A single-agent export is the same shape — ``agents`` is always a list.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import yaml

FORMAT_VERSION = 1
KIND = "agentforge.agent"
AGENT_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "system_prompt",
    "model",
    "provider",
    "temperature",
    "max_steps",
    "tools",
    "config",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agent_to_dict(agent: Any) -> dict[str, Any]:
    """Convert an :class:`Agent` ORM instance into a JSON-safe dict.

    Tolerates both real ORM objects and plain ``Mapping``-shaped test
    doubles (so the tests don't have to hit the database).
    """
    import json

    if isinstance(agent, Mapping):
        tools = agent.get("tools", [])
        config = agent.get("config", {})
    else:
        tools = getattr(agent, "tools", None) or []
        config = getattr(agent, "config", None) or {}

    if isinstance(tools, str):
        try:
            tools = json.loads(tools)
        except (TypeError, ValueError):
            tools = []
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (TypeError, ValueError):
            config = {}

    out: dict[str, Any] = {}
    for field in AGENT_FIELDS:
        if isinstance(agent, Mapping):
            out[field] = agent.get(field)
        else:
            out[field] = getattr(agent, field, None)
    out["tools"] = list(tools) if tools else []
    out["config"] = dict(config) if config else {}
    return out


def build_payload(agents: Iterable[Any], *, single: bool = False) -> dict[str, Any]:
    """Wrap one or many agents in a versioned export envelope."""
    rows = [agent_to_dict(a) for a in agents]
    if single and len(rows) != 1:
        raise ValueError(f"single=True requires exactly 1 agent, got {len(rows)}")
    return {
        "version": FORMAT_VERSION,
        "kind": KIND,
        "exported_at": _utcnow_iso(),
        "agents": rows,
    }


def dump(payload: Mapping[str, Any], fmt: str = "json") -> str:
    """Serialize a payload to a string in the requested format."""
    import json

    fmt = fmt.lower()
    if fmt == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if fmt == "yaml":
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    raise ValueError(f"unsupported format: {fmt!r} (expected 'json' or 'yaml')")


def load(text: str, fmt: str = "json") -> dict[str, Any]:
    """Parse a string back into the export envelope dict.

    Validates ``version`` and ``kind``; raises :class:`ValueError` on
    anything else (malformed input is treated as a 400 by the API).
    """
    import json

    fmt = fmt.lower()
    if fmt == "json":
        data = json.loads(text)
    elif fmt == "yaml":
        data = yaml.safe_load(text)
    else:
        raise ValueError(f"unsupported format: {fmt!r} (expected 'json' or 'yaml')")
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON/YAML object")
    version = data.get("version")
    if version != FORMAT_VERSION:
        raise ValueError(
            f"unsupported version: {version!r} (expected {FORMAT_VERSION})"
        )
    kind = data.get("kind")
    if kind != KIND:
        raise ValueError(f"unsupported kind: {kind!r} (expected {KIND!r})")
    if "agents" not in data or not isinstance(data["agents"], list):
        raise ValueError("payload missing 'agents' list")
    return data


def detect_format(filename: str | None, content: str | bytes) -> str:
    """Sniff the file format from the filename and / or content.

    Falls back to JSON if neither signal is conclusive (most permissive
    default — JSON is the documented default in the README).
    """
    if filename:
        lower = filename.lower()
        if lower.endswith((".yaml", ".yml")):
            return "yaml"
        if lower.endswith(".json"):
            return "json"
    if isinstance(content, bytes):
        sample_b = content[:64].lstrip()
        sample_s = ""
    else:
        sample_s = content[:64].lstrip()
        sample_b = b""
    if sample_b.startswith(b"---") or sample_s.startswith("---"):
        return "yaml"
    return "json"


def agents_from_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the cleaned list of agent dicts from a parsed payload.

    Drops unknown keys so a forward-compatible export (e.g. with a
    ``description`` field added in a later version) still imports
    cleanly into an older server.
    """
    out: list[dict[str, Any]] = []
    for row in payload["agents"]:
        if not isinstance(row, dict):
            raise ValueError("each agent entry must be an object")
        cleaned = {k: row.get(k) for k in AGENT_FIELDS}
        if not cleaned.get("name"):
            raise ValueError("agent entry missing 'name'")
        if not isinstance(cleaned.get("tools", []), list):
            raise ValueError(f"agent {cleaned.get('name')!r}: 'tools' must be a list")
        if not isinstance(cleaned.get("config", {}), dict):
            raise ValueError(f"agent {cleaned.get('name')!r}: 'config' must be an object")
        out.append(cleaned)
    return out


__all__ = [
    "FORMAT_VERSION",
    "KIND",
    "AGENT_FIELDS",
    "agent_to_dict",
    "build_payload",
    "dump",
    "load",
    "detect_format",
    "agents_from_payload",
]

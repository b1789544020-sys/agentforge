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

import copy
import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import yaml

FORMAT_VERSION = 1
CANONICAL_FORMAT_VERSION = 2
SUPPORTED_FORMAT_VERSIONS = frozenset({FORMAT_VERSION, CANONICAL_FORMAT_VERSION})
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
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    from app.secrets import redact_config
    out["config"] = redact_config(dict(config)) if config else {}
    return out


def agent_to_definition(agent: Any) -> dict[str, Any]:
    """Project either an existing public Definition or a legacy Agent DTO."""
    from app.agent.schema import public_definition

    if isinstance(agent, Mapping) and agent.get("schema_version") == 1:
        return public_definition(agent)
    legacy = agent_to_dict(agent)
    return public_definition(legacy)


def _qualified_ref(agent_name: str, ref: str, limit: int) -> str:
    value = f"{agent_name}:{ref}"
    if len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{ref[:limit - 17]}-{digest}"


def _unique_ref(candidate: str, used: set[str], limit: int) -> str:
    output = candidate
    index = 2
    while output in used:
        suffix = f"#{index}"
        output = f"{candidate[:limit - len(suffix)]}{suffix}"
        index += 1
    used.add(output)
    return output


def _disambiguate_package_refs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make logical refs package-unique without exposing local resource IDs."""
    rows = copy.deepcopy(rows)
    kb_counts = Counter(
        item["ref"] for row in rows for item in row["resources"]["knowledge_bases"]
    )
    secret_counts = Counter(item["ref"] for row in rows for item in row["secret_refs"])
    used_kb: set[str] = set()
    used_secrets: set[str] = set()
    for row in rows:
        for item in row["resources"]["knowledge_bases"]:
            original = item["ref"]
            candidate = (
                _qualified_ref(row["name"], original, 128)
                if kb_counts[original] > 1 else original
            )
            item["ref"] = _unique_ref(candidate, used_kb, 128)
        for item in row["secret_refs"]:
            original = item["ref"]
            candidate = (
                _qualified_ref(row["name"], original, 200)
                if secret_counts[original] > 1 else original
            )
            item["ref"] = _unique_ref(candidate, used_secrets, 200)
    return rows


def build_payload(
    agents: Iterable[Any], *, single: bool = False, envelope: int = CANONICAL_FORMAT_VERSION
) -> dict[str, Any]:
    """Wrap agents in canonical envelope 2 or an explicitly lossy legacy envelope."""
    if envelope not in SUPPORTED_FORMAT_VERSIONS:
        raise ValueError(f"unsupported export envelope: {envelope!r}")
    rows = [
        agent_to_dict(agent) if envelope == FORMAT_VERSION else agent_to_definition(agent)
        for agent in agents
    ]
    if single and len(rows) != 1:
        raise ValueError(f"single=True requires exactly 1 agent, got {len(rows)}")
    if envelope == CANONICAL_FORMAT_VERSION:
        rows = _disambiguate_package_refs(rows)
    return {
        "version": envelope,
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
    if version not in SUPPORTED_FORMAT_VERSIONS:
        raise ValueError(
            f"unsupported version: {version!r} (expected 1 or 2)"
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
    """Return legacy-cleaned rows or strictly validated canonical rows.

    Envelope 1 keeps its historical unknown-key dropping behavior. Envelope 2
    instead rejects unknown or higher-version Definition fields so resources
    and extensions cannot be silently discarded.
    """
    if payload.get("version") == CANONICAL_FORMAT_VERSION:
        from app.agent.schema import AgentDefinition
        return [
            AgentDefinition.model_validate(row).model_dump(mode="json")
            for row in payload["agents"]
        ]

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
    "AGENT_FIELDS",
    "CANONICAL_FORMAT_VERSION",
    "FORMAT_VERSION",
    "KIND",
    "SUPPORTED_FORMAT_VERSIONS",
    "agent_to_definition",
    "agent_to_dict",
    "agents_from_payload",
    "build_payload",
    "detect_format",
    "dump",
    "load",
]

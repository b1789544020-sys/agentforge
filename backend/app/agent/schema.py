"""Pure Agent Schema v1 projections and secret-path helpers.

Runtime/ORM snapshots may contain ciphertext. Functions in this module never
return internal secret material and never mutate their inputs.
"""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.secrets import (
    SecretPathConflictError,
    is_sensitive_key,
    looks_like_ciphertext,
    protect_config,
    redact_config,
)

SCHEMA_VERSION = 1
REDACTED = "********"
_MISSING = object()
_POINTER_ESCAPE = re.compile(r"~(?:[^01]|$)")


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class PromptResource(SchemaModel):
    name: str = Field(min_length=1, max_length=200)
    version: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class KnowledgeResource(SchemaModel):
    ref: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)

    @field_validator("ref", "name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ResourceRefs(SchemaModel):
    prompt: PromptResource | None = None
    knowledge_bases: list[KnowledgeResource] = Field(default_factory=list)


class SecretRef(SchemaModel):
    path: str = Field(min_length=1)
    ref: str = Field(min_length=1, max_length=200)
    required: bool = False

    @field_validator("ref")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("path")
    @classmethod
    def _secret_pointer(cls, value: str) -> str:
        tokens = _pointer_tokens(value)
        if len(tokens) < 2 or tokens[0] != "config" or not is_sensitive_key(tokens[-1]):
            raise ValueError("path must identify a sensitive key below /config")
        return value


class AgentDefinition(SchemaModel):
    schema_version: Literal[1]
    name: str = Field(min_length=1, max_length=128)
    description: str | None
    system_prompt: str | None
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    temperature: float = Field(ge=0, le=2)
    max_steps: int = Field(ge=1, le=100)
    tools: list[str]
    config: dict[str, Any]
    resources: ResourceRefs
    secret_refs: list[SecretRef] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _normalized_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("provider", "model")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("tools")
    @classmethod
    def _unique_tools(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("tool names must not be blank")
        if len(set(value)) != len(value):
            raise ValueError("tool names must be unique")
        return value

    @model_validator(mode="after")
    def _consistent_resources_and_secrets(self) -> AgentDefinition:
        kb_refs = [item.ref for item in self.resources.knowledge_bases]
        if len(set(kb_refs)) != len(kb_refs):
            raise ValueError("resources.knowledge_bases.ref values must be unique")
        paths = [item.path for item in self.secret_refs]
        refs = [item.ref for item in self.secret_refs]
        if len(set(paths)) != len(paths) or len(set(refs)) != len(refs):
            raise ValueError("secret_refs path and ref values must be unique")
        by_path = {item.path: item for item in self.secret_refs}
        for local_key in ("prompt_template_id", "prompt_version", "knowledge_base_ids"):
            if local_key in self.config:
                raise ValueError(f"config.{local_key} must be represented by resources")
        for path, value in _sensitive_values(self.config, "/config"):
            if path not in by_path:
                raise ValueError(f"sensitive config path {path} requires a matching secret_ref")
            if looks_like_ciphertext(value):
                raise ValueError(f"ciphertext is not accepted at {path}")
            if value is not None and value != "" and value != REDACTED:
                raise ValueError(f"secret at {path} must be provided through secret_inputs")
        _validate_json_value(self.config, "config")
        _validate_json_value(self.extensions, "extensions")
        return self


class ResourceMap(SchemaModel):
    knowledge_bases: dict[str, str] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)

    @field_validator("knowledge_bases", "prompts")
    @classmethod
    def _non_empty_mappings(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() or not target.strip() for key, target in value.items()):
            raise ValueError("resource map keys and target IDs must not be blank")
        return value


def _validate_json_value(value: Any, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} contains a non-finite number")
    if value is None or isinstance(value, str | bool | int | float):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}.{index}")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} contains a non-JSON value")


def _pointer_tokens(pointer: str) -> list[str]:
    if not pointer.startswith("/") or _POINTER_ESCAPE.search(pointer):
        raise ValueError("path must be a valid RFC 6901 JSON Pointer")
    return [item.replace("~1", "/").replace("~0", "~") for item in pointer[1:].split("/")]


def _sensitive_values(value: Any, path: str) -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}/{_pointer_token(key)}"
            if is_sensitive_key(key):
                yield child, item
            else:
                yield from _sensitive_values(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _sensitive_values(item, f"{path}/{index}")


def _secret_values(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if is_sensitive_key(key) and isinstance(item, str) and item not in {"", REDACTED}:
                found.add(item)
            else:
                found.update(_secret_values(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_secret_values(item))
    return found


def _replace_known_secrets(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, Mapping):
        return {key: _replace_known_secrets(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_known_secrets(item, secrets) for item in value]
    if isinstance(value, str) and value != REDACTED:
        output = value
        for secret in secrets:
            if not secret or looks_like_ciphertext(secret):
                continue
            if output == secret or (len(secret) >= 8 and secret in output):
                output = output.replace(secret, REDACTED)
        return output
    return value


def assert_public_safe(value: Any, *, secret_values: Iterable[str] = ()) -> None:
    """Fail closed if a public DTO still contains known secret material."""
    secrets = {item for item in secret_values if item}

    def visit(item: Any, path: str) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                child_path = f"{path}/{key!s}"
                if is_sensitive_key(key) and child is not None and child != "" and child != REDACTED:
                    raise ValueError(f"unsafe public value at {child_path}")
                visit(child, child_path)
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}/{index}")
            return
        if looks_like_ciphertext(item):
            raise ValueError(f"unsafe public ciphertext at {path}")
        if isinstance(item, str) and item != REDACTED:
            for secret in secrets:
                if item == secret or (len(secret) >= 8 and secret in item):
                    raise ValueError(f"unsafe public secret at {path}")

    visit(value, "")


def public_legacy_dto(value: Any, *, secret_values: Iterable[str] = ()) -> Any:
    """Project any legacy/current response into a uniformly redacted DTO.

    Sensitive values are discovered before redaction so a plaintext fixture or
    an accidentally duplicated secret under a non-sensitive key is also caught.
    """
    known = _secret_values(value)
    known.update(item for item in secret_values if item)
    projected = _replace_known_secrets(redact_config(value), known)
    assert_public_safe(projected, secret_values=known)
    return projected


def _pointer_token(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _logical_ref(pointer: str) -> str:
    tokens = pointer.removeprefix("/config/").split("/")
    return ".".join(token.replace("~1", "/").replace("~0", "~") for token in tokens)


def _strip_secrets(value: Any, path: str = "/config") -> tuple[Any, list[dict[str, Any]]]:
    refs: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            pointer = f"{path}/{_pointer_token(key)}"
            if is_sensitive_key(key):
                # Legacy snapshots have no requiredness metadata; presence is
                # deliberately reported by preflight, not inferred here.
                refs.append({"path": pointer, "ref": _logical_ref(pointer), "required": False})
                continue
            child, child_refs = _strip_secrets(item, pointer)
            output[key] = child
            refs.extend(child_refs)
        return output, refs
    if isinstance(value, list):
        output = []
        for index, item in enumerate(value):
            child, child_refs = _strip_secrets(item, f"{path}/{index}")
            output.append(child)
            refs.extend(child_refs)
        return output, refs
    return value, refs


def _public_resources(resources: Any) -> dict[str, Any]:
    if not isinstance(resources, Mapping):
        return {"prompt": None, "knowledge_bases": []}
    prompt = resources.get("prompt")
    if isinstance(prompt, Mapping):
        prompt_out: dict[str, Any] | None = {
            "name": prompt.get("name"),
            "version": prompt.get("version"),
        }
    else:
        prompt_out = None
    knowledge = []
    rows = resources.get("knowledge_bases")
    if isinstance(rows, list):
        valid_rows = [row for row in rows if isinstance(row, Mapping)]
        if all(row.get("ref") for row in valid_rows):
            valid_rows.sort(key=lambda row: str(row["ref"]))
        else:
            valid_rows.sort(key=lambda row: str(row.get("kb_id") or row.get("id") or ""))
        for index, row in enumerate(valid_rows, 1):
            knowledge.append({"ref": str(row.get("ref") or f"kb-{index}"), "name": row.get("name")})
    return {"prompt": prompt_out, "knowledge_bases": knowledge}


def public_definition(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a portable public Definition without mutating a runtime snapshot."""
    config = snapshot.get("config")
    public_config, secret_refs = _strip_secrets(config if isinstance(config, Mapping) else {})
    for local_key in ("prompt_template_id", "prompt_version", "knowledge_base_ids"):
        public_config.pop(local_key, None)
    previous_refs = {
        row.get("path"): row for row in snapshot.get("secret_refs") or []
        if isinstance(row, Mapping) and row.get("path")
    }
    for ref in secret_refs:
        previous = previous_refs.get(ref["path"])
        if previous is not None:
            ref["ref"] = previous.get("ref") or ref["ref"]
            ref["required"] = bool(previous.get("required", False))
    known_paths = {row["path"] for row in secret_refs}
    for path, previous in previous_refs.items():
        if path not in known_paths:
            secret_refs.append({
                "path": path,
                "ref": previous.get("ref") or _logical_ref(str(path)),
                "required": bool(previous.get("required", False)),
            })
    secret_refs.sort(key=lambda row: row["path"])
    definition = {
        "schema_version": SCHEMA_VERSION,
        "name": snapshot.get("name"),
        "description": snapshot.get("description"),
        "system_prompt": snapshot.get("system_prompt"),
        "provider": snapshot.get("provider"),
        "model": snapshot.get("model"),
        "temperature": snapshot.get("temperature", 0.7),
        "max_steps": snapshot.get("max_steps", 20),
        "tools": list(snapshot.get("tools") or []),
        "config": public_config,
        "resources": _public_resources(snapshot.get("resources")),
        "secret_refs": secret_refs,
        "extensions": dict(snapshot.get("extensions") or {}),
    }
    return public_legacy_dto(definition, secret_values=_secret_values(snapshot))


def masked_secret_report(config: Any, previous: Any = None) -> dict[str, list[dict[str, Any]]]:
    """Classify masked values as inherited or unset without exposing values."""
    missing: list[dict[str, Any]] = []
    inherited: list[dict[str, Any]] = []

    def visit(value: Any, old: Any, path: str) -> None:
        if isinstance(value, Mapping):
            old_mapping = old if isinstance(old, Mapping) else {}
            for key, item in value.items():
                pointer = f"{path}/{_pointer_token(key)}"
                previous_value = old_mapping.get(key)
                if is_sensitive_key(key) and item == REDACTED:
                    record = {
                        "path": pointer, "ref": _logical_ref(pointer), "required": False,
                    }
                    if previous_value is not None and previous_value != "":
                        inherited.append(record)
                    else:
                        missing.append(record)
                elif not is_sensitive_key(key):
                    visit(item, previous_value, pointer)
            return
        if isinstance(value, list):
            old_list = old if isinstance(old, list) else []
            for index, item in enumerate(value):
                visit(item, old_list[index] if index < len(old_list) else None, f"{path}/{index}")

    visit(config, previous, "/config")
    return {
        "missing_secrets": sorted(missing, key=lambda row: row["path"]),
        "inherited_secrets": sorted(inherited, key=lambda row: row["path"]),
    }


def _pointer_get(config: Mapping[str, Any], pointer: str) -> Any:
    tokens = _pointer_tokens(pointer)
    if not tokens or tokens[0] != "config":
        raise ValueError("secret path must start at /config")
    current: Any = config
    for token in tokens[1:]:
        if isinstance(current, Mapping):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list):
            try:
                index = int(token)
            except ValueError:
                return _MISSING
            if str(index) != token or index < 0 or index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _pointer_set(config: dict[str, Any], pointer: str, value: Any) -> None:
    tokens = _pointer_tokens(pointer)[1:]
    if not tokens:
        raise SecretPathConflictError(pointer, "secret path does not identify a value")
    current: Any = config
    for offset, token in enumerate(tokens[:-1], 1):
        if isinstance(current, list):
            try:
                index = int(token)
            except ValueError as exc:
                raise SecretPathConflictError(pointer, "array index is invalid") from exc
            if str(index) != token or index < 0 or index >= len(current):
                raise SecretPathConflictError(pointer, "array length changed")
            current = current[index]
            continue
        if not isinstance(current, dict):
            raise SecretPathConflictError(pointer, "parent type changed")
        if token not in current:
            if tokens[offset].isdigit():
                raise SecretPathConflictError(pointer, "array parent is missing")
            current[token] = {}
        current = current[token]
    final = tokens[-1]
    if isinstance(current, list):
        try:
            index = int(final)
        except ValueError as exc:
            raise SecretPathConflictError(pointer, "array index is invalid") from exc
        if str(index) != final or index < 0 or index >= len(current):
            raise SecretPathConflictError(pointer, "array length changed")
        current[index] = value
    elif isinstance(current, dict):
        current[final] = value
    else:
        raise SecretPathConflictError(pointer, "parent type changed")


def merge_secret_inputs(
    definition: AgentDefinition | Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    secret_inputs: Mapping[str, str | None] | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Merge one-time secrets without returning or persisting their plaintext."""
    model = definition if isinstance(definition, AgentDefinition) else AgentDefinition.model_validate(definition)
    config = copy.deepcopy(model.config)
    previous = previous if isinstance(previous, Mapping) else {}
    inputs = dict(secret_inputs or {})
    refs = [row.model_dump() for row in model.secret_refs]
    known_refs = {row["ref"] for row in refs}
    unknown = sorted(set(inputs) - known_refs)
    if unknown:
        raise ValueError(f"secret_inputs contains unknown refs: {', '.join(unknown)}")
    inherited_paths: set[str] = set()
    for row in refs:
        path, ref = row["path"], row["ref"]
        current = _pointer_get(config, path)
        old = _pointer_get(previous, path)
        if ref in inputs:
            if current is not _MISSING:
                raise ValueError(f"secret at {path} is declared both in config and secret_inputs")
            value = inputs[ref]
            if not isinstance(value, str | type(None)):
                raise ValueError(f"secret input {ref} must be a string or null")
            if looks_like_ciphertext(value):
                raise ValueError(f"ciphertext is not accepted for secret ref {ref}")
            _pointer_set(config, path, value)
        elif current == REDACTED and old is not _MISSING and old is not None and old != "":
            inherited_paths.add(path)
        elif current is _MISSING and old is not _MISSING and old is not None and old != "":
            _pointer_set(config, path, REDACTED)
            inherited_paths.add(path)
    protected = protect_config(config, previous)
    missing: list[dict[str, Any]] = []
    inherited: list[dict[str, Any]] = []
    for row in refs:
        value = _pointer_get(protected, row["path"])
        record = {"path": row["path"], "ref": row["ref"], "required": row["required"]}
        if value is _MISSING or value is None or value == "" or value == REDACTED:
            missing.append(record)
        elif row["path"] in inherited_paths:
            inherited.append(record)
    return protected, {"missing_secrets": missing, "inherited_secrets": inherited}


def runtime_secret_refs(
    config: Mapping[str, Any], previous: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Derive snapshot metadata while preserving required/custom legacy refs."""
    _, found = _strip_secrets(config)
    previous_by_path = {
        str(row["path"]): row for row in previous
        if isinstance(row, Mapping) and row.get("path")
    }
    found_by_path = {row["path"]: row for row in found}
    for path, row in previous_by_path.items():
        target = found_by_path.get(path)
        if target is None:
            target = {"path": path, "ref": row.get("ref") or _logical_ref(path), "required": False}
            found_by_path[path] = target
        target["ref"] = row.get("ref") or target["ref"]
        target["required"] = bool(row.get("required", False))
    return sorted(found_by_path.values(), key=lambda row: row["path"])


def missing_secret_refs(snapshot: Mapping[str, Any], *, required_only: bool = True) -> list[dict[str, Any]]:
    config = snapshot.get("config") if isinstance(snapshot.get("config"), Mapping) else {}
    refs = snapshot.get("secret_refs")
    if not isinstance(refs, list):
        refs = runtime_secret_refs(config)
    missing = []
    for row in refs:
        if not isinstance(row, Mapping) or (required_only and not row.get("required", False)):
            continue
        value = _pointer_get(config, str(row.get("path") or ""))
        if value is _MISSING or value is None or value == "" or value == REDACTED:
            missing.append({
                "path": row.get("path"), "ref": row.get("ref"),
                "required": bool(row.get("required", False)),
            })
    return sorted(missing, key=lambda row: str(row["path"]))


__all__ = [
    "SCHEMA_VERSION",
    "AgentDefinition",
    "KnowledgeResource",
    "PromptResource",
    "ResourceMap",
    "ResourceRefs",
    "SecretRef",
    "assert_public_safe",
    "masked_secret_report",
    "merge_secret_inputs",
    "missing_secret_refs",
    "public_definition",
    "public_legacy_dto",
    "runtime_secret_refs",
]

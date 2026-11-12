"""Small RFC 6901 JSON Pointer implementation with no external dependency."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_INVALID_ESCAPE = re.compile(r"~(?:[^01]|$)")
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


class JsonPointerError(ValueError):
    """Raised when a pointer is malformed or cannot be resolved."""


def pointer_tokens(pointer: str) -> list[str]:
    if not isinstance(pointer, str):
        raise JsonPointerError("JSON Pointer must be a string")
    if pointer == "":
        return []
    if not pointer.startswith("/") or _INVALID_ESCAPE.search(pointer):
        raise JsonPointerError("invalid RFC 6901 JSON Pointer")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def resolve_json_pointer(document: Any, pointer: str) -> Any:
    current = document
    for token in pointer_tokens(pointer):
        if isinstance(current, Mapping):
            if token not in current:
                raise JsonPointerError(f"JSON Pointer key does not exist: {token!r}")
            current = current[token]
            continue
        if isinstance(current, Sequence) and not isinstance(current, str | bytes | bytearray):
            if not _ARRAY_INDEX.fullmatch(token):
                raise JsonPointerError(f"invalid JSON array index: {token!r}")
            index = int(token)
            if index >= len(current):
                raise JsonPointerError(f"JSON array index out of range: {token!r}")
            current = current[index]
            continue
        raise JsonPointerError(f"JSON Pointer cannot descend through scalar at {token!r}")
    return current


__all__ = ["JsonPointerError", "pointer_tokens", "resolve_json_pointer"]

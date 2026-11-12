"""Environment-keyed secret encryption and log/API redaction helpers."""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_PREFIX = "enc:v1:"
_REDACTED = "********"
_SECRET_NAMES = {
    "api_key", "apikey", "token", "secret", "password", "access_token",
    "refresh_token", "client_secret", "secret_key", "access_key",
}


def _fernet() -> Fernet:
    configured = get_settings().encryption_key
    if configured:
        try:
            return Fernet(configured.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("AGENTFORGE_ENCRYPTION_KEY must be a valid Fernet key") from exc
    digest = hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt_secret(value: str) -> str:
    if not value or is_encrypted(value):
        return value
    return _PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not is_encrypted(value):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("encrypted configuration cannot be decrypted with the active key") from exc


def _secret_name(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SECRET_NAMES or normalized.endswith(
        (
            "_api_key", "_token", "_secret", "_password", "_access_key",
            "_api_key_hash", "_token_hash", "_secret_hash", "_password_hash",
        )
    )


def is_sensitive_key(key: Any) -> bool:
    """Return whether a mapping key denotes secret material at any depth."""
    return _secret_name(str(key))


_CIPHERTEXT_PREFIX = re.compile(r"(?i)^(?:enc|cipher|secret):v\d+:")


def looks_like_ciphertext(value: Any) -> bool:
    """Recognize current and forward-version ciphertext without decrypting it."""
    return isinstance(value, str) and bool(_CIPHERTEXT_PREFIX.match(value))


class SecretPathConflictError(ValueError):
    """A masked secret cannot be inherited without ambiguity."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"masked secret at {path} conflicts with previous structure: {reason}")


def _pointer_token(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _contains_mask(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_mask(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_mask(item) for item in value)
    return value == _REDACTED


def _array_identity(value: Any) -> Any:
    if not isinstance(value, dict):
        return None
    for key in ("id", "name", "ref"):
        if key in value and isinstance(value[key], str | int):
            return key, value[key]
    return None


def _protect_config(value: Any, previous: Any, path: str) -> Any:
    has_mask = _contains_mask(value)
    if has_mask and previous is not None:
        compatible = (
            isinstance(value, dict) and isinstance(previous, dict)
        ) or (
            isinstance(value, list) and isinstance(previous, list)
        ) or not isinstance(value, dict | list)
        if not compatible:
            raise SecretPathConflictError(path or "/", "container type changed")
    if isinstance(value, dict):
        previous_dict = previous if isinstance(previous, dict) else {}
        output: dict[str, Any] = {}
        for key, item in value.items():
            child_path = f"{path}/{_pointer_token(key)}"
            old = previous_dict.get(key)
            if _secret_name(str(key)):
                if item == _REDACTED:
                    # A mask without an existing value means unset, never a new secret.
                    if old is not None and old != "":
                        output[key] = old
                elif isinstance(item, str) and item:
                    output[key] = encrypt_secret(item)
                else:
                    output[key] = item
            else:
                output[key] = _protect_config(item, old, child_path)
        return output
    if isinstance(value, list):
        old_list = previous if isinstance(previous, list) else []
        if has_mask and previous is not None and len(value) != len(old_list):
            raise SecretPathConflictError(path or "/", "array length changed")
        output = []
        for index, item in enumerate(value):
            old = old_list[index] if index < len(old_list) else None
            new_identity = _array_identity(item)
            old_identity = _array_identity(old)
            if (
                _contains_mask(item) and new_identity is not None
                and old_identity is not None and new_identity != old_identity
            ):
                raise SecretPathConflictError(f"{path}/{index}", "array identity changed")
            output.append(_protect_config(item, old, f"{path}/{index}"))
        return output
    return value


def protect_config(value: Any, previous: Any = None) -> Any:
    """Recursively encrypt secrets and safely merge masked full replacements.

    ``None`` and the empty string explicitly clear a secret. A mask with no
    previous value remains unset. Structural ambiguity raises
    :class:`SecretPathConflictError` rather than binding a secret to the wrong item.
    """
    return _protect_config(value, previous, "")


def reveal_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: reveal_config(item) for key, item in value.items()}
    if isinstance(value, list):
        return [reveal_config(item) for item in value]
    return decrypt_secret(value) if is_encrypted(value) else value


def redact_config(value: Any) -> Any:
    """Return a recursive public copy with sensitive values and ciphertext masked."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _secret_name(str(key)) and item else redact_config(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return _REDACTED if looks_like_ciphertext(value) else value


_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)(\s*[=:]\s*)([^\s,;]+)")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_CN_ID = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


def redact_text(text: str) -> str:
    text = _BEARER.sub("Bearer [redacted]", str(text))
    text = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
    text = _PHONE.sub("[redacted-phone]", text)
    return _CN_ID.sub("[redacted-id]", text)


__all__ = [
    "SecretPathConflictError", "decrypt_secret", "encrypt_secret", "is_encrypted",
    "is_sensitive_key", "looks_like_ciphertext", "protect_config", "redact_config",
    "redact_text", "reveal_config",
]

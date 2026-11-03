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
        ("_api_key", "_token", "_secret", "_password", "_access_key")
    )


def protect_config(value: Any, previous: Any = None) -> Any:
    """Recursively encrypt sensitive values; redacted updates retain old ciphertext."""
    if isinstance(value, dict):
        previous_dict = previous if isinstance(previous, dict) else {}
        output: dict[str, Any] = {}
        for key, item in value.items():
            old = previous_dict.get(key)
            if _secret_name(str(key)) and item == _REDACTED and is_encrypted(old):
                output[key] = old
            elif _secret_name(str(key)) and isinstance(item, str) and item:
                output[key] = encrypt_secret(item)
            else:
                output[key] = protect_config(item, old)
        return output
    if isinstance(value, list):
        old_list = previous if isinstance(previous, list) else []
        return [protect_config(item, old_list[index] if index < len(old_list) else None)
                for index, item in enumerate(value)]
    return value


def reveal_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: reveal_config(item) for key, item in value.items()}
    if isinstance(value, list):
        return [reveal_config(item) for item in value]
    return decrypt_secret(value) if is_encrypted(value) else value


def redact_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: (_REDACTED if _secret_name(str(key)) and item else redact_config(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return value


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
    "encrypt_secret", "decrypt_secret", "is_encrypted", "protect_config", "reveal_config",
    "redact_config", "redact_text",
]

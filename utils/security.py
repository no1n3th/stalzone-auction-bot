"""Security helpers: masking, constant-time comparison, correlation ids."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Iterable
from typing import Any

SENSITIVE_KEYS: set[str] = {
    "token",
    "access_token",
    "bearer_token",
    "refresh_token",
    "id_token",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "webhook_url",
    "discord_webhook_url",
    "internal_api_token",
    "aws_secret_access_key",
    "dsn",
    "database_url",
}

MASK = "***"


def mask_sensitive(data: Any, sensitive_keys: Iterable[str] | None = None) -> Any:
    """Recursively mask sensitive keys in dicts/lists."""
    keys = {k.lower() for k in (sensitive_keys or SENSITIVE_KEYS)}
    if isinstance(data, dict):
        masked: dict[Any, Any] = {}
        for k, v in data.items():
            if isinstance(k, str) and k.lower() in keys:
                masked[k] = MASK
            else:
                masked[k] = mask_sensitive(v, keys)
        return masked
    if isinstance(data, (list, tuple)):
        return type(data)(mask_sensitive(v, keys) for v in data)
    return data


def constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison (hmac.compare_digest)."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def generate_cid() -> str:
    """UUID4 correlation id."""
    return uuid.uuid4().hex


def mask_text(text: str, secrets: Iterable[str]) -> str:
    """Replace any occurrence of known secret values with ***."""
    if not text:
        return text
    for secret in secrets:
        if secret and len(secret) >= 6 and secret in text:
            text = text.replace(secret, MASK)
    return text

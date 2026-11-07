"""Private per-start Pod and per-capture capabilities."""

from __future__ import annotations

import hashlib
import hmac

from .models import Scope, identifier


def capability(secret: bytes, scope: Scope, start_id: str) -> str:
    if len(secret) < 32:
        raise ValueError("Pod capability master key must contain at least 32 bytes")
    return hmac.new(
        secret,
        f"404-pod/v1\n{scope.key}\n{identifier(start_id)}".encode(),
        hashlib.sha256,
    ).hexdigest()


def capture_token(token: str, exchange_id: str, capture_id: str) -> str:
    data = (
        f"404-capture/v1\n{identifier(exchange_id)}\n{identifier(capture_id)}".encode()
    )
    return hmac.new(bytes.fromhex(token), data, hashlib.sha256).hexdigest()

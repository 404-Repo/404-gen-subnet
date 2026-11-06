"""Private per-start Pod and per-capture capabilities."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from .models import Scope, identifier, json_bytes


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


def miner_access(token: str, scope: Scope, start_id: str, pod_id: str, expires: int) -> str:
    payload = base64.urlsafe_b64encode(json_bytes({
        "scope": scope.key, "start_id": identifier(start_id), "pod_id": pod_id, "expires": expires,
    })).decode()
    signature = hmac.new(bytes.fromhex(token), b"404-miner/v1\n" + payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def verify_miner_access(value: str, token: str, scope: Scope, start_id: str, pod_id: str) -> bool:
    try:
        payload = json.loads(base64.urlsafe_b64decode(value.split(".", 1)[0]))
        expires = payload["expires"]
        return (type(expires) is int and time.time() < expires
                and hmac.compare_digest(value, miner_access(token, scope, start_id, pod_id, expires)))
    except (ValueError, KeyError, TypeError):
        return False

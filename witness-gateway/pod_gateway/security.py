"""Private, per-start Pod capabilities and permanent drain seals."""

from __future__ import annotations

import hashlib
import hmac
import json
import re

from .models import Scope, identifier, json_bytes, object_dict
from .state import StateConflict, StateStore


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


def pod_state_key(scope: Scope, start_id: str, pod_id: str, kind: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pod_id) or kind not in (
        "drain",
        "request-drain",
    ):
        raise ValueError("Invalid Pod state identity")
    return f"pod-control/v1/{scope.deployment}/{scope.round}/{scope.hotkey}/{identifier(start_id)}/{pod_id}/{kind}.json"


def seal(
    store: StateStore, scope: Scope, start_id: str, pod_id: str, token: str, kind: str
) -> None:
    key = pod_state_key(scope, start_id, pod_id, kind)
    data = json_bytes(
        {
            "key": key,
            "mac": hmac.new(
                bytes.fromhex(token), key.encode(), hashlib.sha256
            ).hexdigest(),
        }
    )
    try:
        store.save(key, data, None)
    except StateConflict:
        if not sealed(store, scope, start_id, pod_id, token, kind):
            raise ValueError("Conflicting Pod drain seal") from None


def sealed(
    store: StateStore,
    scope: Scope,
    start_id: str,
    pod_id: str,
    token: str,
    kind: str = "drain",
) -> bool:
    key = pod_state_key(scope, start_id, pod_id, kind)
    saved = store.load(key)
    if saved is None:
        return False
    value = object_dict(json.loads(saved.data))
    expected = hmac.new(bytes.fromhex(token), key.encode(), hashlib.sha256).hexdigest()
    return (
        value.get("key") == key
        and isinstance(value.get("mac"), str)
        and hmac.compare_digest(str(value["mac"]), expected)
    )

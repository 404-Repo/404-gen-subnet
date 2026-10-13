"""Authenticate the public miner protocol and its signed exchange request."""

from __future__ import annotations

import hashlib
import re
from typing import cast

from js import URL
from bindings import HttpRequest

from models import Env, Exchange, GatewayRequest, Rejection
from runtime import bounded_bytes, crypto_bridge, dumps, loads, now_ms, safe_integer

WITNESS_ID = "404-witness"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def exchange_payload(job: Exchange) -> str:
    """Use the worker identity verifier's domain and canonical field order."""
    fields: dict[str, str | int] = {
        "operation": job["operation"],
        "hotkey": job["hotkey"],
        "receiver": job["receiver"],
        "exchange_id": job["exchange_id"],
        "deadline": job["deadline"],
        "provider": job["provider"],
    }
    if job.get("replace"):
        fields["replace"] = job["replace"]
    return "404-witness-queue/v1\n" + dumps(fields)


async def registered(hotkey: str, env: Env) -> None:
    raw = await env.REGISTRY.get("membership.json")
    try:
        snapshot = loads(raw) if raw and len(raw) <= 512 * 1024 else None
    except ValueError:
        raise Rejection(503, "membership_unavailable") from None
    now = now_ms()
    if (
        not isinstance(snapshot, dict)
        or not safe_integer(snapshot.get("version"))
        or snapshot.get("version") != 1
        or snapshot.get("network") != env.NETWORK
        or snapshot.get("netuid") != int(env.NETUID)
        or not safe_integer(snapshot.get("block"))
        or cast(int, snapshot["block"]) < 0
        or not isinstance(snapshot.get("hotkeys"), list)
    ):
        raise Rejection(503, "membership_unavailable")
    hotkeys = cast(list[object], snapshot["hotkeys"])
    if len(hotkeys) > 4096:
        raise Rejection(503, "membership_unavailable")
    for name in ("fetched_at", "block_time"):
        value = snapshot.get(name)
        if (
            not safe_integer(value)
            or cast(int, value) > now + 30_000
            or now - cast(int, value) > 300_000
        ):
            raise Rejection(503, "membership_unavailable")
    if env.ROUND == "test-0" and hotkey == getattr(env, "TEST_HOTKEY", ""):
        return
    if hotkey not in hotkeys:
        raise Rejection(403, "hotkey_not_registered")


async def authenticate(request: HttpRequest, env: Env) -> GatewayRequest:
    url = URL.new(request.url)
    path: str = url.pathname
    status = (
        re.fullmatch(r"/status/([0-9a-f-]{36})", path)
        if request.method == "GET"
        else None
    )
    object_route = (
        re.fullmatch(
            r"/exchanges/([0-9a-f-]{36})/(connect|progress|response\.sse)", path
        )
        if request.method == "GET"
        else None
    )
    release = (
        re.fullmatch(r"/release/([0-9a-f-]{36})", path)
        if request.method == "POST"
        else None
    )
    route = status or object_route or release
    if url.search or not (
        request.method == "POST"
        and path == "/run"
        or route
        and UUID.fullmatch(route[1])
    ):
        raise Rejection(404, "unknown_route")
    headers = request.headers
    hotkey = headers.get("x-bittensor-hotkey") or ""
    crypto = crypto_bridge()
    try:
        key = crypto.publicKey(hotkey)
    except Exception:
        raise Rejection(401, "invalid_hotkey") from None
    nonce = headers.get("x-bittensor-nonce") or ""
    signature = headers.get("x-bittensor-signature") or ""
    algorithm = headers.get("x-bittensor-crypto")
    if (
        headers.get("x-bittensor-version") != "1"
        or headers.get("x-bittensor-receiver") != WITNESS_ID
        or (isinstance(algorithm, str) and algorithm != "sr25519")
        or not re.fullmatch(r"[1-9][0-9]{15,19}", nonce)
        or not re.fullmatch(r"0x[0-9a-f]{128}", signature)
    ):
        raise Rejection(401, "invalid_hotkey_auth")
    age = now_ms() * 1_000_000 - int(nonce)
    if age > 10_000_000_000 or age < -2_000_000_000:
        raise Rejection(401, "stale_hotkey_auth")
    body = await bounded_bytes(
        request.body, 4096 if request.method == "POST" and not release else 0
    )
    message = "\n".join(
        (
            "btauth/1",
            "sr25519",
            request.method,
            path,
            hashlib.sha256(body).hexdigest(),
            nonce,
            hotkey,
            WITNESS_ID,
        )
    )
    if not await crypto.verify(message, signature[2:], key):
        raise Rejection(401, "invalid_signature")
    await registered(hotkey, env)
    if route:
        result: GatewayRequest = {
            "hotkey": hotkey,
            "nonce": nonce,
            "requestId": route[1],
        }
        if object_route:
            if object_route[2] == "connect":
                result["connect"] = True
            else:
                result["object"] = (
                    "status.json" if object_route[2] == "progress" else "response.sse"
                )
        if release:
            result["release"] = True
        return result
    try:
        data = loads(body.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeError):
        raise Rejection(400, "invalid_job") from None
    required = {
        "operation",
        "hotkey",
        "receiver",
        "exchange_id",
        "deadline",
        "provider",
        "signature",
    }
    if not isinstance(data, dict) or data.keys() - {"replace"} != required:
        raise Rejection(400, "invalid_job")
    replacement = data.get("replace")
    if "replace" in data and (
        not isinstance(replacement, str)
        or not UUID.fullmatch(replacement)
        or replacement == data["exchange_id"]
    ):
        raise Rejection(400, "invalid_job")
    if (
        data["operation"] != "exchange"
        or data["hotkey"] != hotkey
        or data["receiver"] != WITNESS_ID
        or data["provider"] not in ("api", "codex")
        or not isinstance(data["exchange_id"], str)
        or not UUID.fullmatch(data["exchange_id"])
        or not safe_integer(data["deadline"])
        or cast(int, data["deadline"]) <= now_ms() / 1000
        or cast(int, data["deadline"]) > now_ms() / 1000 + 604801
        or not isinstance(data["signature"], str)
        or not re.fullmatch(r"[0-9a-f]{128}", data["signature"])
    ):
        raise Rejection(400, "invalid_job")
    job = cast(Exchange, data)
    job["deadline"] = int(job["deadline"])
    if not await crypto.verify(exchange_payload(job), job["signature"], key):
        raise Rejection(401, "invalid_signature")
    return {
        "hotkey": hotkey,
        "nonce": nonce,
        "requestId": job["exchange_id"],
        "exchange": job,
        "origin": url.origin,
    }

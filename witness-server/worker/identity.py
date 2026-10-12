"""Bind queued jobs and transcript paths to a signed miner identity."""

from __future__ import annotations

import re
import time
import uuid
from typing import Literal
from urllib.parse import urlsplit

from bittensor_wallet import Keypair

from storage import json_bytes

WITNESS_ID = "404-witness"
Provider = Literal["api", "codex"]
FIELDS = ("operation", "hotkey", "receiver", "exchange_id", "deadline", "provider")


def public_key(hotkey: str) -> Keypair:
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{48}", hotkey):
        raise ValueError("Invalid miner hotkey")
    key = Keypair(ss58_address=hotkey, ss58_format=42)
    raw = key.public_key
    if (
        raw is None
        or len(raw) != 32
        or Keypair(public_key=raw.hex(), ss58_format=42).ss58_address != hotkey
    ):
        raise ValueError("Expected a canonical Bittensor hotkey")
    return key


def validate_round(round_number: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", round_number):
        raise ValueError("Round must be 1–64 lowercase letters, digits, or hyphens")


def miner_prefix(round_number: str, hotkey: str) -> str:
    validate_round(round_number)
    public_key(hotkey)
    return f"rounds/{round_number}/{hotkey}"


def payload(data: dict[str, object]) -> bytes:
    # A separate domain from HTTP login. The deadline covers queue wait time.
    return b"404-witness-queue/v1\n" + json_bytes(
        {
            **{key: data[key] for key in FIELDS},
            **({"replace": data["replace"]} if "replace" in data else {}),
        }
    )


def signed_exchange(
    hotkey: Keypair, identifier: str, deadline: int, *, provider: Provider
) -> dict[str, object]:
    public_key(hotkey.ss58_address)
    if hotkey.crypto_type != 1:
        raise ValueError("Expected an sr25519 Bittensor hotkey")
    if provider not in ("api", "codex"):
        raise ValueError("Unknown witness provider")
    data: dict[str, object] = {
        "operation": "exchange",
        "hotkey": hotkey.ss58_address,
        "receiver": WITNESS_ID,
        "exchange_id": str(uuid.UUID(identifier)),
        "deadline": deadline,
        "provider": provider,
    }
    data["signature"] = hotkey.sign(payload(data)).hex()
    return data


def authenticate_exchange(
    data: dict[str, object], round_number: str
) -> tuple[str, str, Provider]:
    # The gateway adds the operator's round after verifying the miner's signature.
    if (
        set(data) - {"replace", "gateway", "token", "expires"}
        != {*FIELDS, "signature", "round"}
        or data.get("operation") != "exchange"
    ):
        raise ValueError("Invalid signed exchange")
    if data["round"] != round_number or data["receiver"] != WITNESS_ID:
        raise ValueError("Wrong witness or round")
    if data["provider"] not in ("api", "codex"):
        raise ValueError("Unknown witness provider")
    hotkey, identifier, signature, deadline = (
        data[name] for name in ("hotkey", "exchange_id", "signature", "deadline")
    )
    if (
        not isinstance(hotkey, str)
        or not isinstance(identifier, str)
        or not isinstance(signature, str)
        or not re.fullmatch(r"[0-9a-f]{128}", signature)
    ):
        raise ValueError("Invalid signed exchange")
    if type(deadline) is not int or not 0 < deadline - time.time() <= 604801:
        raise ValueError("Expired or excessive exchange deadline")
    miner_prefix(round_number, hotkey)
    if str(uuid.UUID(identifier)) != identifier:
        raise ValueError("Invalid exchange identifier")
    if not public_key(hotkey).verify(payload(data), bytes.fromhex(signature)):
        raise ValueError("Invalid miner signature")
    return hotkey, identifier, data["provider"]


def worker_connection(data: dict[str, object]) -> tuple[str, str, int]:
    """Validate the private gateway capability supplied in a RunPod job."""
    gateway, token, expires = (
        data.get(name) for name in ("gateway", "token", "expires")
    )
    if (
        not isinstance(gateway, str)
        or not isinstance(token, str)
        or not re.fullmatch(r"[0-9a-f]{64}", token)
    ):
        raise ValueError("Missing worker connection capability")
    url = urlsplit(gateway)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.path
        or url.query
        or url.fragment
        or url.username
    ):
        raise ValueError("Invalid gateway origin")
    if (
        type(expires) is not int
        or not 0 < expires - time.time() <= 604801
        or expires > int(str(data["deadline"]))
    ):
        raise ValueError("Invalid worker deadline")
    return (
        f"wss://{url.netloc}/workers/{data['round']}/{data['hotkey']}/{data['exchange_id']}",
        token,
        expires,
    )

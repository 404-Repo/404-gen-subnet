"""Validate miner identities and round-scoped transcript paths."""

from __future__ import annotations

import re
from typing import Literal

from bittensor_wallet import Keypair

Provider = Literal["api", "codex"]


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

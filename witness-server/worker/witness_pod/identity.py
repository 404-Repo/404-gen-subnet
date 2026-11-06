"""Validate miner identities and round-scoped transcript paths."""

from __future__ import annotations

import re
from typing import Literal

from subnet_common.witness.auth import AuthRejected, keypair_from_hotkey

Provider = Literal["api", "codex"]


def validate_round(round_number: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", round_number):
        raise ValueError("Round must be 1–64 lowercase letters, digits, or hyphens")


def miner_prefix(round_number: str, hotkey: str) -> str:
    validate_round(round_number)
    try:
        keypair_from_hotkey(hotkey)
    except AuthRejected:
        raise ValueError("Invalid miner hotkey") from None
    return f"rounds/{round_number}/{hotkey}"

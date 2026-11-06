"""Shared btauth/1 signature verification and nonce tracking."""

from __future__ import annotations

import hashlib
import heapq
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping

from bittensor_wallet import Keypair
from bittensor_wallet.utils import get_ss58_format


class AuthRejected(RuntimeError):
    def __init__(self, code: str, status: int = 401) -> None:
        super().__init__(code)
        self.status = status


def keypair_from_hotkey(hotkey: str) -> Keypair:
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{48}", hotkey):
        raise AuthRejected("invalid_hotkey")
    try:
        if get_ss58_format(hotkey) != 42:
            raise ValueError("Expected Bittensor SS58 format 42")
        return Keypair(ss58_address=hotkey, ss58_format=42)
    except Exception:  # noqa: BLE001 - wallet codecs can raise native exceptions
        raise AuthRejected("invalid_hotkey") from None


class Authenticator:
    def __init__(self, authorize: Callable[[str], None]) -> None:
        self.authorize = authorize
        self._nonces: dict[tuple[str, str], int] = {}
        self._expiry: list[tuple[int, str, str]] = []
        self._counts: dict[str, int] = {}
        self._keys: OrderedDict[str, Keypair] = OrderedDict()
        self._lock = threading.Lock()

    def _check_nonce(self, hotkey: str, nonce: str, now: int) -> None:
        while self._expiry and self._expiry[0][0] <= now:
            _, owner, expired = heapq.heappop(self._expiry)
            self._nonces.pop((owner, expired), None)
            self._counts[owner] -= 1
            if self._counts[owner] == 0:
                del self._counts[owner]
        if (hotkey, nonce) in self._nonces:
            raise AuthRejected("replayed_hotkey_auth")
        if len(self._nonces) >= 100_000 or self._counts.get(hotkey, 0) >= 256:
            raise AuthRejected("authentication_busy", 429)

    def verify(
        self, method: str, path: str, body: bytes, headers: Mapping[str, str]
    ) -> str:
        hotkey = headers.get("x-bittensor-hotkey", "")
        nonce, signature = (
            headers.get("x-bittensor-nonce", ""),
            headers.get("x-bittensor-signature", ""),
        )
        if (
            headers.get("x-bittensor-version") != "1"
            or headers.get("x-bittensor-receiver") != "404-witness"
            or headers.get("x-bittensor-crypto", "sr25519") != "sr25519"
            or re.fullmatch(r"[1-9][0-9]{15,19}", nonce) is None
            or re.fullmatch(r"0x[0-9a-f]{128}", signature) is None
        ):
            raise AuthRejected("invalid_hotkey_auth")
        now = time.time_ns()
        if not -30_000_000_000 <= now - int(nonce) <= 30_000_000_000:
            raise AuthRejected("stale_hotkey_auth")
        self.authorize(hotkey)
        with self._lock:
            self._check_nonce(hotkey, nonce, now)
            key = self._keys.get(hotkey)
        if key is None:
            key = keypair_from_hotkey(hotkey)
        payload = "\n".join(
            [
                "btauth/1",
                "sr25519",
                method,
                path,
                hashlib.sha256(body).hexdigest(),
                nonce,
                hotkey,
                "404-witness",
            ]
        )
        if not key.verify(payload.encode(), bytes.fromhex(signature[2:])):
            raise AuthRejected("invalid_signature")
        self.authorize(hotkey)
        with self._lock:
            self._check_nonce(hotkey, nonce, now)
            expiry = int(nonce) + 30_000_000_001
            self._nonces[(hotkey, nonce)] = expiry
            heapq.heappush(self._expiry, (expiry, hotkey, nonce))
            self._counts[hotkey] = self._counts.get(hotkey, 0) + 1
            self._keys[hotkey] = key
            self._keys.move_to_end(hotkey)
            if len(self._keys) > 4096:
                self._keys.popitem(last=False)
        return hotkey

"""Existing btauth/1 signatures and finalized Finney membership in Python."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping
from typing import cast

import httpx
import xxhash
from bittensor_wallet import Keypair

from .models import json_bytes, object_dict, string

FINNEY_GENESIS = "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"


class AuthRejected(RuntimeError):
    def __init__(self, code: str, status: int = 401) -> None:
        super().__init__(code)
        self.status = status


def public_key(hotkey: str) -> Keypair:
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{48}", hotkey):
        raise AuthRejected("invalid_hotkey")
    try:
        key = Keypair(ss58_address=hotkey, ss58_format=42)
        raw = key.public_key
        if (
            raw is None
            or len(raw) != 32
            or Keypair(public_key=raw.hex(), ss58_format=42).ss58_address != hotkey
        ):
            raise ValueError("Noncanonical hotkey")
        return key
    except Exception:  # noqa: BLE001 - wallet codecs can raise native exceptions
        raise AuthRejected("invalid_hotkey") from None


class Membership:
    def __init__(
        self,
        deployment: str,
        netuid: int,
        round_number: str,
        test_hotkey: str | None = None,
    ) -> None:
        if not 0 <= netuid <= 65535 or (
            test_hotkey is not None and round_number not in {"test-0", "test-1", "test-2"}
        ):
            raise ValueError("Invalid membership configuration")
        self.netuid, self.test_hotkey = netuid, test_hotkey
        self.snapshot: dict[str, object] | None = None

    def require(self, hotkey: str) -> None:
        value = self.snapshot
        if value is None:
            raise AuthRejected("membership_unavailable", 503)
        now = int(time.time() * 1000)
        if (
            value.get("version") != 1
            or value.get("network") != "finney"
            or value.get("netuid") != self.netuid
            or type(value.get("block")) is not int
            or cast(int, value["block"]) < 0
            or not isinstance(value.get("hotkeys"), list)
            or len(cast(list[object], value["hotkeys"])) > 4096
            or any(
                type(value.get(k)) is not int
                or not now - 48 * 3600_000 <= cast(int, value[k]) <= now + 30_000
                for k in ("fetched_at", "block_time")
            )
        ):
            raise AuthRejected("membership_unavailable", 503)
        if hotkey != self.test_hotkey and hotkey not in cast(
            list[object], value["hotkeys"]
        ):
            raise AuthRejected("hotkey_not_registered", 403)


class Authenticator:
    def __init__(self, membership: Membership) -> None:
        self.membership = membership
        self._nonces: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def verify(
        self, method: str, path: str, body: bytes, headers: Mapping[str, str]
    ) -> str:
        hotkey = headers.get("x-bittensor-hotkey", "")
        key = public_key(hotkey)
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
        if not -2_000_000_000 <= now - int(nonce) <= 10_000_000_000:
            raise AuthRejected("stale_hotkey_auth")
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
        self.membership.require(hotkey)
        with self._lock:
            self._nonces = {
                k: expiry for k, expiry in self._nonces.items() if expiry > now
            }
            if (hotkey, nonce) in self._nonces:
                raise AuthRejected("replayed_hotkey_auth")
            if len(self._nonces) >= 100_000:
                raise AuthRejected("authentication_busy", 429)
            self._nonces[(hotkey, nonce)] = int(nonce) + 10_000_000_001
        return hotkey


def storage_key(pallet: str, name: str) -> str:
    def twox(value: str) -> str:
        return b"".join(
            xxhash.xxh64(value.encode(), seed=i).intdigest().to_bytes(8, "little")
            for i in (0, 1)
        ).hex()

    return "0x" + twox(pallet) + twox(name)


class MembershipRefresher:
    def __init__(
        self, membership: Membership, client: httpx.Client, rpc_url: str
    ) -> None:
        if not rpc_url.startswith("https://"):
            raise ValueError("Subtensor RPC must use HTTPS")
        self.membership, self.client, self.url = membership, client, rpc_url

    def _rpc(self, method: str, params: list[object]) -> object:
        response = self.client.post(
            self.url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            follow_redirects=False,
        )
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            raise ValueError("Chain RPC unavailable")
        value = object_dict(response.json())
        if value.get("id") != 1 or value.get("jsonrpc") != "2.0" or "error" in value:
            raise ValueError("Invalid chain RPC reply")
        return value["result"]

    def _storage(self, keys: list[str], block: str) -> list[str]:
        result = self._rpc("state_queryStorageAt", [keys, block])
        if not isinstance(result, list) or len(result) != 1:
            raise ValueError("Invalid storage reply")
        row = object_dict(result[0])
        changes = row.get("changes")
        if (
            row.get("block") != block
            or not isinstance(changes, list)
            or len(changes) != len(keys)
        ):
            raise ValueError("Incomplete membership snapshot")
        values: dict[str, str] = {}
        for item in changes:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or item[0] not in keys
                or item[0] in values
            ):
                raise ValueError("Invalid membership storage entry")
            values[string(item[0])] = string(item[1])
        return [values[k] for k in keys]

    def refresh(self) -> None:
        if self._rpc("chain_getBlockHash", [0]) != FINNEY_GENESIS:
            raise ValueError("RPC is not Finney")
        block = string(self._rpc("chain_getFinalizedHead", []))
        if re.fullmatch(r"0x[0-9a-f]{64}", block) is None:
            raise ValueError("Invalid finalized block")
        height = string(object_dict(self._rpc("chain_getHeader", [block]))["number"])
        netuid = self.membership.netuid.to_bytes(2, "little").hex()
        timestamp, count = self._storage(
            [
                storage_key("Timestamp", "Now"),
                storage_key("SubtensorModule", "SubnetworkN") + netuid,
            ],
            block,
        )
        if (
            re.fullmatch(r"0x[0-9a-f]{16}", timestamp) is None
            or re.fullmatch(r"0x[0-9a-f]{4}", count) is None
        ):
            raise ValueError("Invalid membership counters")
        block_time, size = (
            int.from_bytes(bytes.fromhex(timestamp[2:]), "little"),
            int.from_bytes(bytes.fromhex(count[2:]), "little"),
        )
        now = int(time.time() * 1000)
        if not now - 300_000 <= block_time <= now + 30_000 or size > 4096:
            raise ValueError("Stale finalized membership")
        prefix = storage_key("SubtensorModule", "Keys") + netuid
        hotkeys: list[str] = []
        for start in range(0, size, 256):
            keys = [
                prefix + i.to_bytes(2, "little").hex()
                for i in range(start, min(start + 256, size))
            ]
            for value in self._storage(keys, block):
                if re.fullmatch(r"0x[0-9a-f]{64}", value) is None:
                    raise ValueError("Invalid registered public key")
                hotkeys.append(
                    Keypair(public_key=value[2:], ss58_format=42).ss58_address
                )
        if len(set(hotkeys)) != size:
            raise ValueError("Duplicate registered hotkeys")
        data = json_bytes(
            {
                "version": 1,
                "network": "finney",
                "netuid": self.membership.netuid,
                "block": int(height, 16),
                "block_hash": block,
                "block_time": block_time,
                "fetched_at": now,
                "hotkeys": sorted(hotkeys),
                "revision": str(time.time_ns()),
            }
        )
        self.membership.snapshot = object_dict(json.loads(data))

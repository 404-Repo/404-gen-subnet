"""Gateway Finney registration snapshots and chain queries."""

from __future__ import annotations

import json
import re
import time
from typing import cast

import httpx
import xxhash
from bittensor_wallet import Keypair
from subnet_common.witness.auth import AuthRejected
from subnet_common.witness.models import json_bytes, object_dict, string

FINNEY_GENESIS = "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"


class Membership:
    def __init__(
        self,
        netuid: int,
        round_number: str,
        test_hotkey: str | None = None,
        *,
        admitted_hotkeys: frozenset[str] | None = None,
    ) -> None:
        if not 0 <= netuid <= 65535 or (
            test_hotkey is not None and round_number not in {"test-0", "test-1", "test-2"}
        ):
            raise ValueError("Invalid membership configuration")
        self.netuid, self.test_hotkey = netuid, test_hotkey
        self.admitted_hotkeys = admitted_hotkeys
        self.snapshot: dict[str, object] | None = None

    def require(self, hotkey: str) -> None:
        if self.admitted_hotkeys is not None and hotkey not in self.admitted_hotkeys:
            raise AuthRejected("hotkey_not_admitted", 403)
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

    def generation_head(self) -> tuple[int, float]:
        """Read a timestamped current Finney block for generation deadlines."""
        if self._rpc("chain_getBlockHash", [0]) != FINNEY_GENESIS:
            raise ValueError("RPC is not Finney")
        block = string(self._rpc("chain_getBlockHash", []))
        if re.fullmatch(r"0x[0-9a-f]{64}", block) is None:
            raise ValueError("Invalid generation block")
        height = string(object_dict(self._rpc("chain_getHeader", [block]))["number"])
        timestamp = self._storage([storage_key("Timestamp", "Now")], block)[0]
        if re.fullmatch(r"0x[0-9a-f]{16}", timestamp) is None or re.fullmatch(r"0x[0-9a-f]+", height) is None:
            raise ValueError("Invalid generation block timestamp")
        return int(height, 16), int.from_bytes(bytes.fromhex(timestamp[2:]), "little") / 1000

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

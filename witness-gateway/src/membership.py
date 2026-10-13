"""Publish complete registration snapshots from one finalized Finney block."""

from __future__ import annotations

import re
from typing import cast

from js import AbortSignal, URL, fetch
from pyodide.ffi import JsProxy
from workers import WorkerEntrypoint

from models import Json, Membership, MembershipEnv
from runtime import (
    bounded_text,
    crypto_bridge,
    dumps,
    js_object,
    loads,
    now_ms,
    safe_integer,
)

FINNEY_GENESIS = "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"


def record(value: Json) -> dict[str, Json]:
    if not isinstance(value, dict):
        raise ValueError("Invalid chain reply")
    return value


def hex_bytes(value: Json, length: int) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(
        rf"0x[0-9a-f]{{{length * 2}}}", value
    ):
        raise ValueError("Invalid chain value")
    return bytes.fromhex(value[2:])


def little_endian(value: Json, length: int) -> int:
    number = int.from_bytes(hex_bytes(value, length), "little")
    if not safe_integer(number):
        raise ValueError("Chain value exceeds integer precision")
    return number


def u16(value: int) -> str:
    return value.to_bytes(2, "little").hex()


class SubtensorRpc:
    """Read-only RPC with one timeout shared by the complete refresh."""

    def __init__(self, url: str, signal: object) -> None:
        self.url = url
        self.signal = signal

    async def call(self, method: str, params: list[Json] | None = None) -> Json:
        response = await fetch(
            self.url,
            js_object(
                {
                    "method": "POST",
                    "redirect": "manual",
                    "signal": self.signal,
                    "headers": {"content-type": "application/json"},
                    "body": dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": method,
                            "params": params or [],
                        }
                    ),
                }
            ),
        )
        if not response.ok:
            if response.body:
                await response.body.cancel()
            raise ValueError(f"Chain RPC HTTP {response.status}")
        reply = record(loads(await bounded_text(response.body, 1024 * 1024)))
        if (
            reply.get("jsonrpc") != "2.0"
            or not safe_integer(reply.get("id"))
            or reply.get("id") != 1
            or "error" in reply
            or "result" not in reply
        ):
            raise ValueError("Chain RPC failed")
        return reply["result"]

    async def storage(self, keys: list[str], block: str) -> list[str]:
        result = await self.call(
            "state_queryStorageAt", [cast(list[Json], keys), block]
        )
        if not isinstance(result, list) or len(result) != 1:
            raise ValueError("Invalid chain storage reply")
        entry = record(result[0])
        changes = entry.get("changes")
        if (
            entry.get("block") != block
            or not isinstance(changes, list)
            or len(changes) != len(keys)
        ):
            raise ValueError("Incomplete chain storage reply")
        expected, values = set(keys), {}
        for change in changes:
            if (
                not isinstance(change, list)
                or len(change) != 2
                or not isinstance(change[0], str)
                or not isinstance(change[1], str)
                or change[0] not in expected
                or change[0] in values
            ):
                raise ValueError("Invalid chain storage entry")
            values[change[0]] = change[1]
        return [values[key] for key in keys]


async def read_membership(env: MembershipEnv) -> Membership:
    netuid = int(env.NETUID)
    if (
        env.NETWORK != "finney"
        or not 0 <= netuid <= 65535
        or URL.new(env.SUBTENSOR_RPC_URL).protocol != "https:"
    ):
        raise ValueError("Invalid membership configuration")
    rpc = SubtensorRpc(env.SUBTENSOR_RPC_URL, AbortSignal.timeout(45_000))
    genesis = await rpc.call("chain_getBlockHash", [0])
    if genesis != FINNEY_GENESIS:
        raise ValueError("RPC is not connected to Finney")
    block_hash = await rpc.call("chain_getFinalizedHead")
    hex_bytes(block_hash, 32)
    block_hash = cast(str, block_hash)
    crypto = crypto_bridge()
    header = await rpc.call("chain_getHeader", [block_hash])
    values = await rpc.storage(
        [
            crypto.storageKey("Timestamp", "Now"),
            crypto.storageKey("SubtensorModule", "SubnetworkN") + u16(netuid),
        ],
        block_hash,
    )
    height = record(header).get("number")
    if not isinstance(height, str) or not re.fullmatch(r"0x[0-9a-f]+", height):
        raise ValueError("Invalid block height")
    block = int(height, 16)
    block_time, count = little_endian(values[0], 8), little_endian(values[1], 2)
    if not safe_integer(block) or count > 4096:
        raise ValueError("Invalid membership size or block")
    prefix = crypto.storageKey("SubtensorModule", "Keys") + u16(netuid)
    hotkeys: list[str] = []
    for start in range(0, count, 256):
        keys = [prefix + u16(uid) for uid in range(start, min(start + 256, count))]
        for key in await rpc.storage(keys, block_hash):
            hotkeys.append(crypto.encodeHotkey(hex_bytes(key, 32).hex()))
    now = now_ms()
    if block_time > now + 30_000 or now - block_time > 300_000:
        raise ValueError("Finalized block is stale")
    if len(set(hotkeys)) != count:
        raise ValueError("Duplicate registered hotkeys")
    return {
        "version": 1,
        "network": "finney",
        "netuid": netuid,
        "block": block,
        "block_hash": block_hash,
        "block_time": block_time,
        "fetched_at": now,
        "hotkeys": sorted(hotkeys),
    }


async def refresh_membership(env: MembershipEnv) -> None:
    snapshot = await read_membership(env)
    await env.REGISTRY.put("membership.json", dumps(snapshot))
    print(
        dumps(
            {
                "event": "membership_refreshed",
                "netuid": snapshot["netuid"],
                "block": snapshot["block"],
                "members": len(snapshot["hotkeys"]),
            }
        )
    )


class Default(WorkerEntrypoint):
    env: MembershipEnv

    async def scheduled(
        self, controller: JsProxy, env: MembershipEnv, ctx: JsProxy
    ) -> None:
        try:
            await refresh_membership(self.env)
        except Exception as error:
            print(
                dumps(
                    {
                        "event": "membership_refresh_failed",
                        "network": self.env.NETWORK,
                        "netuid": self.env.NETUID,
                        "error": str(error),
                    }
                )
            )
            # Leave the saved snapshot untouched and retry on the next cron tick.

"""Replacement for ``subtensor.get_all_revealed_commitments``, which crashes on bittensor 10.x.

The bittensor implementation assumes every commitment is a ``0x`` hex string and calls
``bytes.fromhex()``, but the runtime returns an already-decoded ``str`` whenever the
payload is valid UTF-8 (raising "non-hexadecimal number found in fromhex()").
Delete this module once the bug is fixed upstream.
"""

import bittensor as bt


async def get_all_revealed_commitments(
    subtensor: bt.AsyncSubtensor, netuid: int
) -> dict[str, tuple[tuple[int, str], ...]]:
    """Retrieve all revealed commitments for a subnet, keyed by hotkey."""
    query = await subtensor.query_map(module="Commitments", name="RevealedCommitments", params=[netuid])
    result: dict[str, tuple[tuple[int, str], ...]] = {}
    async for hotkey, data in query:
        result[hotkey] = tuple(_decode_revealed_commitment(p[0], p[1]) for p in data)
    return result


def _decode_revealed_commitment(commitment: object, block: int) -> tuple[int, str]:
    """Decode a single commitment, tolerant of both shapes the chain returns,
    then strip the SCALE compact-length prefix the same way bittensor does.
    """
    if isinstance(commitment, str) and commitment.startswith("0x"):
        raw = bytes.fromhex(commitment[2:])
    elif isinstance(commitment, str):
        raw = commitment.encode("utf-8", errors="ignore")
    else:
        raw = bytes(commitment)  # type: ignore[call-overload]
    if not raw:
        return block, ""
    mode = raw[0] & 0b11
    offset = 1 if mode == 0 else 2 if mode == 1 else 4
    return block, raw[offset:].decode("utf-8", errors="ignore")

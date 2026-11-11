"""Miner entry point: the runner supplies one image, seed and async witness."""

from __future__ import annotations

from typing import TYPE_CHECKING

from miner_reference.pipeline import run

if TYPE_CHECKING:
    from witness_client import AsyncWitnessClient


async def generate(image: bytes, seed: int, witness: AsyncWitnessClient) -> str | None:
    # Use the round's seed for random choices. The validator gets the same seed
    # when it runs the miner again, so it can make the same choices.
    return await run(image, seed, witness)

"""Miner entry point: the sandbox supplies one image, seed and clients."""

from __future__ import annotations

from typing import TYPE_CHECKING

from miner_reference.pipeline import run

if TYPE_CHECKING:
    from renderer_client import Renderer
    from witness_client import WitnessClient


def generate(image: bytes, seed: int, witness: WitnessClient, renderer: Renderer) -> str | None:
    # Use the round's seed for random choices. The validator gets the same seed
    # when it runs the miner again, so it can make the same choices.
    return run(image, seed, witness.responses, renderer)

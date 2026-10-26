"""Miner entry point: the sandbox supplies one image, seed and clients."""

from __future__ import annotations

from typing import TYPE_CHECKING

from miner_reference.pipeline import run

if TYPE_CHECKING:
    from renderer_client import Renderer
    from witness_client import WitnessClient


def generate(image: bytes, seed: int, witness: WitnessClient, renderer: Renderer) -> str | None:
    """Generate JavaScript using the round seed for local candidate ordering.

    Generation and replay receive the same seed to reproduce that ordering.
    """
    return run(image, seed, witness.responses, renderer)

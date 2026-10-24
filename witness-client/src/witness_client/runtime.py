"""Development integration for generate(image, seed, witness, renderer).

Production isolation belongs to the separate runner: expected recordings and
the replay engine must remain outside untrusted miner execution.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TypeVar

from .client import WitnessClient
from .replay import ReplayWitness

R = TypeVar("R")


def run_generation(
    generate: Callable[[bytes, int, WitnessClient, R], str | None],
    image: bytes,
    seed: int,
    witness: WitnessClient,
    renderer: R,
    *,
    prompt_url: str | None = None,
) -> str | None:
    bound = witness.for_generation(image, seed, prompt_url=prompt_url)
    output = generate(image, seed, bound, renderer)
    if output is not None and not isinstance(output, str):
        raise ValueError("generate() must return JavaScript or None")
    bound.finish(output)
    return output


def run_replay(
    generate: Callable[[bytes, int, WitnessClient, R], str | None],
    image: bytes,
    seed: int,
    witness: ReplayWitness,
    renderer: R,
) -> str | None:
    generation = witness.recording.generation
    if hashlib.sha256(image).hexdigest() != generation.prompt_sha256 or seed != generation.seed:
        raise ValueError("Replay prompt or generation seed differs")
    output = generate(image, seed, witness, renderer)
    if output is not None and not isinstance(output, str):
        raise ValueError("generate() must return JavaScript or None")
    witness.finish(output)
    return output

"""Development execution host for generate(image, seed, witness, renderer).

The caller must isolate observations and clients from untrusted code. These
helpers do not provide a sandbox, environment freeze or renderer determinism.
"""

from __future__ import annotations

import fcntl
import hashlib
import uuid
from collections.abc import Callable
from typing import TypeVar

from .calls import RequestKey
from .client import CapturedResponse, WitnessClient
from .errors import ReplayMismatch
from .observations import Execution, Inputs, Observation, ObservedWitness, Schedule
from .replay import ReplayWitness

R = TypeVar("R")


class _CaptureWitness(ObservedWitness):
    def __init__(self, parent: WitnessClient, schedule: Schedule) -> None:
        super().__init__(schedule)
        self.parent = parent

    def read(
        self, entry: Observation, replay: bool, key: RequestKey, body: bytes
    ) -> CapturedResponse:
        if replay:
            if entry.exchange_id is None:
                raise ReplayMismatch("Completed observation has no capture")
            return self.parent._saved_response(entry.exchange_id)
        return self.parent._resolve(key, body, key.session_id)


def run_generation(
    generate: Callable[[bytes, int, WitnessClient, R], str | None],
    image: bytes,
    seed: int,
    witness: WitnessClient,
    renderer: R,
) -> str | None:
    inputs = Inputs.supplied(image, seed)
    namespace = hashlib.sha256(
        f"{witness._http.base_url}/{witness._hotkey.ss58_address}/"
        f"{witness._config['round']}/{witness._mode}".encode()
    ).hexdigest()
    directory = witness._state / "executions" / namespace / inputs.id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "execution.json"
    with (directory / "execution.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReplayMismatch("This execution is already running") from None
        execution = (
            Execution.model_validate_json(path.read_bytes())
            if path.is_file()
            else Execution(inputs=inputs, provider=witness._mode)
        )
        if execution.aborted:
            path.rename(directory / ("aborted-" + str(uuid.uuid4()) + ".json"))
            execution = Execution(inputs=inputs, provider=witness._mode)
        if execution.inputs != inputs or execution.provider != witness._mode:
            raise ReplayMismatch("Frozen execution inputs differ")
        schedule = Schedule(
            execution, path=path, timeout=witness._gateway.delivery_timeout
        )
        schedule.persist()
        try:
            output = generate(image, seed, _CaptureWitness(witness, schedule), renderer)
            if output is not None and not isinstance(output, str):
                raise ValueError("generate() must return JavaScript or None")
            schedule.finish(output)
            return output
        except BaseException:
            if not execution.finished:
                execution.aborted = True
                schedule.persist()
            raise


def run_replay(
    generate: Callable[[bytes, int, WitnessClient, R], str | None],
    image: bytes,
    seed: int,
    witness: ReplayWitness,
    renderer: R,
) -> str | None:
    if Inputs.supplied(image, seed) != witness.schedule.execution.inputs:
        raise ReplayMismatch("Replay image or generation seed differs")
    output = generate(image, seed, witness, renderer)
    if output is not None and not isinstance(output, str):
        raise ValueError("generate() must return JavaScript or None")
    witness.finish(output)
    return output

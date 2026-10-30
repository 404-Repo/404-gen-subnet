"""Runner execution host for synchronous and async miner entry points.

The caller must isolate observations and clients from untrusted code. This
host does not provide a sandbox, environment freeze or renderer determinism.
"""

from __future__ import annotations

import fcntl
import uuid
from typing import Protocol, TypeVar

from witness_client import CaptureContext, CapturedResponse
from witness_client.calls import RequestKey

from .observations import (
    Execution,
    Generate,
    Inputs,
    Observation,
    ObservedWitness,
    ReplayMismatch,
    Schedule,
    invoke_generate,
)

R = TypeVar("R")


class CaptureSource(Protocol):
    """Capture metadata and retained model outcomes supplied to the runner."""

    @property
    def capture_context(self) -> CaptureContext: ...

    def capture_response(self, key: RequestKey, body: bytes) -> CapturedResponse: ...

    def saved_response(self, exchange_id: str) -> CapturedResponse: ...


class _CaptureWitness(ObservedWitness):
    def __init__(self, parent: CaptureSource, schedule: Schedule) -> None:
        super().__init__(schedule)
        self.parent = parent

    def read(
        self, entry: Observation, replay: bool, key: RequestKey, body: bytes
    ) -> CapturedResponse:
        if replay:
            if entry.exchange_id is None:
                raise ReplayMismatch("Completed observation has no capture")
            return self.parent.saved_response(entry.exchange_id)
        return self.parent.capture_response(key, body)


def run_generation(
    generate: Generate[R],
    image: bytes,
    seed: int,
    witness: CaptureSource,
    renderer: R,
) -> str | None:
    inputs = Inputs.supplied(image, seed)
    context = witness.capture_context
    directory = context.state_directory / "executions" / context.namespace / inputs.id
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
            else Execution(inputs=inputs, provider=context.provider)
        )
        if execution.aborted:
            path.rename(directory / ("aborted-" + str(uuid.uuid4()) + ".json"))
            execution = Execution(inputs=inputs, provider=context.provider)
        if execution.inputs != inputs or execution.provider != context.provider:
            raise ReplayMismatch("Frozen execution inputs differ")
        schedule = Schedule(
            execution, path=path, timeout=context.delivery_timeout
        )
        schedule.persist()
        try:
            output = invoke_generate(generate, image, seed, _CaptureWitness(witness, schedule), renderer)
            if output is not None and not isinstance(output, str):
                raise ValueError("generate() must return JavaScript or None")
            schedule.finish(output)
            return output
        except BaseException:
            if not execution.finished:
                execution.aborted = True
                schedule.persist()
            raise

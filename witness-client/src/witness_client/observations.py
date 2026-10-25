"""Execution-host observations, separate from provider capture and prompt accounting."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar, Unpack, cast, overload

from openai.types.responses import Response, ResponseStreamEvent
from openai.types.responses.response_create_params import ResponseCreateParamsBase
from pydantic import BaseModel, ConfigDict, Field

from .calls import Digest, RequestKey, canonical
from .client import CapturedResponse, WitnessClient
from .errors import ExchangePending, ReplayMismatch, WitnessError
from .response import ModelStream, model_result
from .state import save

T = TypeVar("T")


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    image_sha256: Digest
    seed: int

    @classmethod
    def supplied(cls, image: bytes, seed: int) -> Inputs:
        return cls(image_sha256=hashlib.sha256(image).hexdigest(), seed=seed)

    @property
    def id(self) -> str:
        return hashlib.sha256(canonical(self.model_dump())).hexdigest()


class Failure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str
    exchange_id: str | None
    status_code: int | None
    code: str | None
    source: Literal["client", "gateway", "provider"]
    pending: bool = False

    @classmethod
    def observed(cls, error: WitnessError) -> Failure:
        return cls(
            message=str(error),
            exchange_id=error.exchange_id,
            status_code=error.status_code,
            code=error.code,
            source=error.source,
            pending=isinstance(error, ExchangePending),
        )

    def restore(self) -> WitnessError:
        if self.pending and self.exchange_id is not None:
            return ExchangePending(
                self.message, self.exchange_id, code=self.code or "capture_in_progress"
            )
        return WitnessError(
            self.message,
            self.exchange_id,
            status_code=self.status_code,
            code=self.code,
            source=self.source,
        )


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request: RequestKey
    occurrence: int = Field(ge=0)
    action: Literal["create", "next", "close"]
    index: int = Field(ge=0)
    stream: bool
    entered: int = Field(ge=0)
    exited: int | None = None
    outcome: str | None = None
    exchange_id: str | None = None
    failure: Failure | None = None

    @property
    def identity(self) -> tuple[str, int, str, int]:
        return self.request.id, self.occurrence, self.action, self.index


class Execution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[2] = 2
    inputs: Inputs
    provider: Literal["api", "codex"]
    observations: list[Observation] = Field(default_factory=list)
    finished: bool = False
    aborted: bool = False
    output_sha256: Digest | None = None
    output_is_none: bool = False


class Schedule:
    """Match per-key occurrences and restore causal observation order."""

    def __init__(
        self, execution: Execution, *, path: Path | None = None, timeout: float = 30
    ) -> None:
        if timeout <= 0:
            raise ValueError("Replay timeout must be positive")
        self.execution, self.path, self.timeout = execution, path, timeout
        self.condition = threading.Condition()
        self.steps = {entry.identity: entry for entry in execution.observations}
        times = [
            number
            for entry in execution.observations
            for number in (entry.entered, entry.exited)
            if number is not None
        ]
        if (
            len(self.steps) != len(execution.observations)
            or len(times) != len(set(times))
            or sorted(times) != list(range(len(times)))
            or any(
                entry.exited is not None and entry.exited <= entry.entered
                for entry in execution.observations
            )
            or (
                execution.finished
                and any(entry.exited is None for entry in execution.observations)
            )
        ):
            raise ReplayMismatch("Execution observation order is invalid")
        self.tick = len(times)
        self.saved = set(self.steps)
        self.started: set[tuple[str, int, str, int]] = set()
        self.done: set[tuple[str, int, str, int]] = set()
        self.counts: dict[str, int] = {}
        self.failure: str | None = None

    def persist(self) -> None:
        if self.path is not None:
            save(self.path, self.execution.model_dump_json().encode())

    def mismatch(self, message: str) -> None:
        self.failure = message
        self.condition.notify_all()
        raise ReplayMismatch(message)

    def wait(self, predicate: Callable[[], bool], deadline: float) -> None:
        while not predicate():
            if self.failure is not None:
                raise ReplayMismatch(self.failure)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.mismatch(
                    "Execution has missing work or incompatible observation order"
                )
            self.condition.wait(remaining)

    def occurrence(self, key: RequestKey) -> int:
        with self.condition:
            value = self.counts.get(key.id, 0)
            self.counts[key.id] = value + 1
            return value

    def step(
        self,
        key: RequestKey,
        occurrence: int,
        action: Literal["create", "next", "close"],
        index: int,
        stream: bool,
        operation: Callable[[Observation, bool], T],
    ) -> T:
        identity = (key.id, occurrence, action, index)
        deadline = time.monotonic() + self.timeout
        with self.condition:
            if self.failure is not None:
                raise ReplayMismatch(self.failure)
            entry = self.steps.get(identity)
            if entry is None:
                if self.path is None or self.execution.finished:
                    self.mismatch(
                        "Execution changed a request, request_id, stream operation or occurrence"
                    )
                self.wait(lambda: self.saved <= self.done, deadline)
                entry = Observation(
                    request=key,
                    occurrence=occurrence,
                    action=action,
                    index=index,
                    stream=stream,
                    entered=self.tick,
                )
                self.tick += 1
                self.steps[identity] = entry
                self.execution.observations.append(entry)
                self.persist()
            if entry.stream != stream or identity in self.started:
                self.mismatch("Execution changed its model-call interface")
            replay = entry.exited is not None
            self.wait(
                lambda: all(
                    other.identity in self.done
                    for other in self.steps.values()
                    if other.exited is not None and other.exited < entry.entered
                ),
                deadline,
            )
            self.started.add(identity)
            self.condition.notify_all()
        result: T
        error: BaseException | None = None
        try:
            if replay and entry.failure is not None:
                raise entry.failure.restore()
            result = operation(entry, replay)
        except BaseException as caught:  # noqa: BLE001 - persist every observable boundary outcome
            error = caught
        with self.condition:
            outcome = "return" if error is None else type(error).__name__
            if replay:
                if entry.outcome != outcome:
                    self.mismatch("Retained observation produced a different outcome")
                self.wait(
                    lambda: all(
                        (
                            other.identity in self.started
                            if other.entered < cast(int, entry.exited)
                            else True
                        )
                        and (
                            other.identity in self.done
                            if other.exited is not None
                            and other.exited < cast(int, entry.exited)
                            else True
                        )
                        for other in self.steps.values()
                    ),
                    deadline,
                )
            else:
                entry.exited, entry.outcome = self.tick, outcome
                self.tick += 1
                if error is not None and isinstance(error, WitnessError):
                    entry.failure = Failure.observed(error)
                    entry.exchange_id = error.exchange_id
                elif error is None and isinstance(result, CapturedResponse):
                    entry.exchange_id = result.exchange_id
                self.persist()
            self.done.add(identity)
            self.condition.notify_all()
        if error is not None:
            raise error
        return result

    def finish(self, output: str | None) -> None:
        with self.condition:
            if self.failure is not None or len(self.done) != len(self.steps):
                self.mismatch("Execution did not consume its observations")
            digest = hashlib.sha256((output or "").encode()).hexdigest()
            if self.execution.finished:
                if (
                    self.execution.output_sha256 != digest
                    or self.execution.output_is_none != (output is None)
                ):
                    self.mismatch("Execution output differs")
            else:
                self.execution.finished, self.execution.output_sha256 = True, digest
                self.execution.output_is_none = output is None
                self.persist()


class ObservedStream(ModelStream):
    def __init__(
        self, stream: ModelStream, schedule: Schedule, key: RequestKey, occurrence: int
    ) -> None:
        self.stream, self.schedule, self.key, self.occurrence = (
            stream,
            schedule,
            key,
            occurrence,
        )
        self.index = 0
        self.closed = False

    def __next__(self) -> ResponseStreamEvent:
        index, self.index = self.index, self.index + 1
        return self.schedule.step(
            self.key, self.occurrence, "next", index, True, lambda *_: next(self.stream)
        )

    def close(self) -> None:
        if self.closed:
            return
        self.schedule.step(
            self.key,
            self.occurrence,
            "close",
            self.index,
            True,
            lambda *_: self.stream.close(),
        )
        self.closed = True


class ObservedWitness(WitnessClient):
    """Private execution boundary; submitted code only calls the Responses interface."""

    def __init__(self, schedule: Schedule) -> None:
        self.schedule = schedule
        self._mode = schedule.execution.provider
        self.responses = self

    def read(
        self, entry: Observation, replay: bool, key: RequestKey, body: bytes
    ) -> CapturedResponse:
        raise NotImplementedError

    @overload
    def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[False] = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response: ...

    @overload
    def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[True],
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> ModelStream: ...

    def create(
        self,
        *,
        request_id: int = 0,
        stream: bool = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response | ModelStream:
        key, body = self._request(request_id, session_id, params)
        occurrence = self.schedule.occurrence(key)
        captured = self.schedule.step(
            key,
            occurrence,
            "create",
            0,
            stream,
            lambda entry, replay: self.read(entry, replay, key, body),
        )
        result = model_result(captured.status, captured.body, self._mode, stream=stream)
        return (
            ObservedStream(result, self.schedule, key, occurrence)
            if isinstance(result, ModelStream)
            else result
        )

    def close(self) -> None:
        pass

    def resume(self, exchange_id: str) -> Response:
        raise ReplayMismatch("Execution code must use responses.create()")

    def release(self, exchange_id: str) -> None:
        raise ReplayMismatch("Execution code cannot change capture allocations")

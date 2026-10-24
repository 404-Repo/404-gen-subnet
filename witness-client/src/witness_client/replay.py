"""Offline model replay from an operator-verified recording bundle."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Literal, Unpack, cast

from openai.types.responses.response_create_params import (
    ResponseCreateParamsBase,
    ResponseCreateParamsStreaming,
)

from .calls import RequestKey
from .client import WitnessClient, WitnessResult, recorded_result
from .errors import WitnessError
from .pods import RecordingView
from .request import serialize_body, validate_session_id


class ReplayMismatch(WitnessError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="replay_mismatch")


class ReplayWitness(WitnessClient):
    """Restore invocation and delivery order without a network/inference client.

    The trusted runner owns the bundle, outside miner scratch space. Export and
    verify it with pod_gateway.replay before constructing this interface.
    Ordering is enforced at the model-call boundary, not at the OS scheduler.
    """

    def __init__(self, directory: str | Path, *, timeout: float = 30) -> None:
        self.directory = Path(directory)
        self.recording = RecordingView.model_validate_json(
            (self.directory / "recording.json").read_bytes()
        )
        self.bundle = json.loads((self.directory / "verified.json").read_bytes())
        self.mode = cast(Literal["api", "codex"], self.bundle["provider"])
        if self.mode not in ("api", "codex") or timeout <= 0:
            raise ValueError("Invalid replay configuration")
        if (
            not self.recording.finished
            or self.bundle.get("recording_sha256")
            != hashlib.sha256(
                (self.directory / "recording.json").read_bytes()
            ).hexdigest()
            or sorted(
                e.delivery for e in self.recording.entries if e.delivery is not None
            )
            != list(range(len(self.recording.entries)))
            or any(call.phase != "issued" for call in self.recording.calls.values())
        ):
            raise ReplayMismatch("Recording is incomplete or its order is invalid")
        self.responses = self
        self.timeout = timeout
        self.condition = threading.Condition()
        self.cursor = self.delivery = 0
        self.failure: str | None = None

    def _mismatch(self, message: str) -> None:
        self.failure = message
        self.condition.notify_all()
        raise ReplayMismatch(message)

    def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[True] = True,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> WitnessResult:
        validate_session_id(session_id, self.mode)
        normalized = cast(
            ResponseCreateParamsStreaming, {"service_tier": "default", **params, "stream": stream}
        )
        key = RequestKey.from_body(
            self.mode, serialize_body(normalized, self.mode), request_id
        )
        deadline = time.monotonic() + self.timeout
        with self.condition:
            # A thread arriving early may wait for the recorded earlier thread.
            # Sequential reordering cannot skip the expected invocation.
            while True:
                if self.failure is not None:
                    raise ReplayMismatch(self.failure)
                if self.cursor >= len(self.recording.entries):
                    self._mismatch("Replay made an extra model call")
                entry = self.recording.entries[self.cursor]
                if entry.call_id == key.id:
                    self.cursor += 1
                    self.condition.notify_all()
                    break
                if not any(
                    e.call_id == key.id for e in self.recording.entries[self.cursor :]
                ):
                    self._mismatch("Replay changed a model request or request_id")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._mismatch("Replay model invocation order differs")
                self.condition.wait(remaining)
        call = self.recording.calls[entry.call_id]
        directory = self.directory / call.exchange_id
        manifest = (directory / "manifest.json").read_bytes()
        response = (directory / "response.sse").read_bytes()
        metadata = json.loads(manifest)
        if (
            hashlib.sha256(manifest).hexdigest() != call.manifest_sha256
            or metadata.get("response_sha256") != hashlib.sha256(response).hexdigest()
            or metadata.get("call_id") != key.id
            or metadata.get("generation_id") != self.recording.generation.id
        ):
            with self.condition:
                self._mismatch("Replay evidence changed after verification")
        result: WitnessResult | None = None
        failure: WitnessError | None = None
        try:
            result = recorded_result(
                self.bundle["hotkey"],
                call.exchange_id,
                metadata["provider_status"],
                response,
            )
        except WitnessError as error:
            failure = error
        with self.condition:
            while entry.delivery != self.delivery:
                if self.failure is not None:
                    raise ReplayMismatch(self.failure)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._mismatch("Replay model delivery order differs")
                self.condition.wait(remaining)
            self.delivery += 1
            self.condition.notify_all()
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def finish(self, output: str | None) -> None:
        with self.condition:
            if (
                self.failure is not None
                or self.cursor != len(self.recording.entries)
                or self.delivery != self.cursor
            ):
                self._mismatch("Replay did not consume the complete recording")
            if (
                self.recording.output_is_none != (output is None)
                or self.recording.output_sha256
                != hashlib.sha256((output or "").encode()).hexdigest()
            ):
                self._mismatch("Replay output differs from generation")

    def close(self) -> None:
        pass

    def release(self, exchange_id: str) -> None:
        raise ReplayMismatch("Replay cannot change control-plane allocations")

    def resume(self, exchange_id: str) -> WitnessResult:
        raise ReplayMismatch(
            "Replay consumes recorded create() calls through its ordered cursor"
        )

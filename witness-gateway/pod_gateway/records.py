"""Durable model-call deduplication and invocation/delivery ordering in R2."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from witness_client.calls import Generation, Invocation, RequestKey

from .models import Scope, identifier, json_bytes, object_dict
from .state import StateConflict, StateStore

T = TypeVar("T")


class RecordingConflict(RuntimeError):
    pass


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request: RequestKey
    exchange_id: str
    owner_ordinal: int
    deadline: int
    phase: Literal["reserved", "assigned", "capturing", "issued", "failed"] = "reserved"
    start_id: str | None = None
    pod_id: str | None = None
    capture_id: str | None = None
    authorized: bool = False
    # Only terminal worker evidence or release before claim proves non-dispatch.
    not_dispatched: bool = False
    manifest_sha256: str | None = None
    failure: str | None = None


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    call_id: str
    delivery: int | None = None


class Recording(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    revision: str
    scope_key: str
    generation: Generation
    entries: list[Entry] = Field(default_factory=list)
    calls: dict[str, Call] = Field(default_factory=dict)
    attempts: dict[str, Call] = Field(default_factory=dict)
    output_sha256: str | None = None
    output_is_none: bool = False
    finished: bool = False


class CallRecords:
    def __init__(self, store: StateStore, *, exchange_seconds: int = 1800) -> None:
        if not 60 <= exchange_seconds <= 86400:
            raise ValueError("Invalid exchange deadline")
        self.store = store
        self.exchange_seconds = exchange_seconds

    @staticmethod
    def _original(recording: Recording, call: Call) -> Call:
        if call.authorized or call.phase == "issued":
            return call
        return next(
            (
                previous for previous in recording.attempts.values()
                if previous.request == call.request
                and (previous.authorized or not previous.not_dispatched)
            ),
            call,
        )

    @staticmethod
    def key(scope: Scope, generation_id: str) -> str:
        if len(generation_id) != 64 or any(
            c not in "0123456789abcdef" for c in generation_id
        ):
            raise ValueError("Invalid generation ID")
        return f"pod-recordings/v1/{scope.deployment}/{scope.round}/{scope.hotkey}/{generation_id}.json"

    def load(self, scope: Scope, generation_id: str) -> Recording | None:
        snapshot = self.store.load(self.key(scope, generation_id))
        if snapshot is None:
            return None
        recording = Recording.model_validate_json(snapshot.data)
        if recording.scope_key != scope.key or recording.generation.id != generation_id:
            raise RecordingConflict("Recording identity differs")
        return recording

    def _change(
        self, scope: Scope, generation: Generation, change: Callable[[Recording], T]
    ) -> T:
        key = self.key(scope, generation.id)
        for _ in range(16):
            snapshot = self.store.load(key)
            recording = (
                Recording.model_validate_json(snapshot.data)
                if snapshot is not None
                else Recording(
                    revision=str(uuid.uuid4()),
                    scope_key=scope.key,
                    generation=generation,
                )
            )
            if (
                recording.scope_key != scope.key
                or recording.generation.id != generation.id
            ):
                raise RecordingConflict("Recording identity differs")
            result = change(recording)
            recording.revision = str(uuid.uuid4())
            try:
                self.store.save(
                    key,
                    recording.model_dump_json().encode(),
                    None if snapshot is None else snapshot.etag,
                )
                return result
            except StateConflict:
                continue
        raise StateConflict("Recording is busy")

    def reserve(self, scope: Scope, invocation: Invocation) -> Call:
        if invocation.generation.round != scope.round:
            raise RecordingConflict("Generation belongs to a different round")

        def change(recording: Recording) -> Call:
            index, key = invocation.ordinal, invocation.request.id
            if index < len(recording.entries):
                if recording.entries[index].call_id != key:
                    raise RecordingConflict("Replay request or call order differs")
                current = self._original(recording, recording.calls[key])
                if current.phase == "reserved" and current.deadline <= time.time():
                    current.deadline = int(time.time()) + self.exchange_seconds
                return current.model_copy(deep=True)
            if recording.finished or index != len(recording.entries):
                raise RecordingConflict("Missing, extra or out-of-order invocation")
            if key not in recording.calls:
                exchange_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{scope.key}/{invocation.generation.id}/{key}",
                    )
                )
                recording.calls[key] = Call(
                    request=invocation.request,
                    exchange_id=exchange_id,
                    owner_ordinal=index,
                    deadline=int(time.time()) + self.exchange_seconds,
                )
            recording.entries.append(Entry(call_id=key))
            return self._original(recording, recording.calls[key]).model_copy(deep=True)

        call = self._change(scope, invocation.generation, change)
        self._index(scope, invocation.generation, call)
        return call

    def _index(self, scope: Scope, generation: Generation, call: Call) -> None:
        key = self.index_key(scope, call.exchange_id)
        data = json_bytes(
            {
                "generation_id": generation.id,
                "call_id": call.request.id,
            }
        )
        try:
            self.store.save(key, data, None)
        except StateConflict:
            saved = self.store.load(key)
            if saved is None or saved.data != data:
                raise RecordingConflict("Exchange index conflicts") from None

    def retry(self, scope: Scope, invocation: Invocation, exchange_id: str) -> Call:
        def change(recording: Recording) -> Call:
            key = invocation.request.id
            if recording.entries[invocation.ordinal].call_id != key:
                raise RecordingConflict("Retry differs from recorded invocation")
            current = self._original(recording, recording.calls[key])
            if current.exchange_id != exchange_id or current.phase != "failed":
                return current.model_copy(deep=True)
            if recording.finished or any(
                entry.call_id == key and entry.delivery is not None for entry in recording.entries
            ):
                raise RecordingConflict("A delivered outcome cannot be retried")
            if not current.not_dispatched or current.authorized:
                return current.model_copy(deep=True)
            recording.attempts[current.exchange_id] = current.model_copy(deep=True)
            recording.calls[key] = Call(
                request=current.request,
                exchange_id=str(uuid.uuid4()),
                owner_ordinal=current.owner_ordinal,
                deadline=int(time.time()) + self.exchange_seconds,
            )
            return recording.calls[key].model_copy(deep=True)

        call = self._change(scope, invocation.generation, change)
        self._index(scope, invocation.generation, call)
        return call

    @staticmethod
    def index_key(scope: Scope, exchange_id: str) -> str:
        return f"pod-exchanges/v1/{scope.deployment}/{scope.round}/{scope.hotkey}/{identifier(exchange_id)}.json"

    def locate(self, scope: Scope, exchange_id: str) -> tuple[Invocation, Call] | None:
        import json

        saved = self.store.load(self.index_key(scope, exchange_id))
        if saved is None:
            return None
        index = object_dict(json.loads(saved.data))
        recording = self.load(scope, str(index["generation_id"]))
        if recording is None:
            raise RecordingConflict("Exchange recording is missing")
        call = recording.calls[str(index["call_id"])]
        if call.exchange_id != exchange_id:
            previous = recording.attempts.get(exchange_id)
            if previous is None or previous.request != call.request:
                raise RecordingConflict("Exchange identity differs")
            call = previous
        return Invocation(
            generation=recording.generation,
            ordinal=call.owner_ordinal,
            request=call.request,
        ), call

    def assign(
        self,
        scope: Scope,
        invocation: Invocation,
        start_id: str,
        pod_id: str,
        *,
        allow_reassign: bool = False,
    ) -> Call:
        def change(recording: Recording) -> Call:
            call = self._original(recording, recording.calls[invocation.request.id])
            if call.phase == "reserved" or (
                call.phase == "assigned" and allow_reassign
            ):
                call.phase, call.start_id, call.pod_id = "assigned", start_id, pod_id
                call.capture_id = str(uuid.uuid4())
            return call.model_copy(deep=True)

        return self._change(scope, invocation.generation, change)

    def claim(
        self, scope: Scope, invocation: Invocation, pod_id: str, capture_id: str
    ) -> bool:
        def change(recording: Recording) -> bool:
            call = recording.calls[invocation.request.id]
            if (
                call.phase != "assigned"
                or call.pod_id != pod_id
                or call.capture_id != capture_id
                or self._original(recording, call).exchange_id != call.exchange_id
            ):
                return False
            call.phase = "capturing"
            return True

        return self._change(scope, invocation.generation, change)

    def authorize(self, scope: Scope, invocation: Invocation, pod_id: str, *, exchange_id: str) -> None:
        def change(recording: Recording) -> None:
            call = recording.calls[invocation.request.id]
            if (
                call.exchange_id != exchange_id or call.phase != "capturing"
                or call.pod_id != pod_id or call.authorized
                or self._original(recording, call).exchange_id != call.exchange_id
            ):
                raise RecordingConflict(
                    "Capture is no longer eligible for authorization"
                )
            call.authorized = True

        self._change(scope, invocation.generation, change)

    def issued(
        self, scope: Scope, invocation: Invocation, pod_id: str, manifest: bytes, *, exchange_id: str
    ) -> None:
        digest = hashlib.sha256(manifest).hexdigest()

        def change(recording: Recording) -> None:
            call = recording.calls[invocation.request.id]
            if call.exchange_id != exchange_id:
                raise RecordingConflict("Capture attempt was superseded")
            if call.phase == "issued" and call.manifest_sha256 == digest:
                return
            if (
                call.phase != "capturing"
                or not call.authorized
                or call.pod_id != pod_id
            ):
                raise RecordingConflict("Capture cannot issue this result")
            call.phase, call.manifest_sha256 = "issued", digest

        self._change(scope, invocation.generation, change)

    def failed(
        self, scope: Scope, invocation: Invocation, pod_id: str, failure: str, *, exchange_id: str,
        not_dispatched: bool = False,
    ) -> None:
        def change(recording: Recording) -> None:
            call = recording.calls[invocation.request.id]
            if call.exchange_id != exchange_id:
                previous = recording.attempts.get(exchange_id)
                if previous is not None and previous.pod_id == pod_id and previous.failure == failure:
                    return
                raise RecordingConflict("Capture attempt was superseded")
            if call.phase == "failed" and call.failure == failure:
                return
            if call.phase != "capturing" or call.pod_id != pod_id:
                raise RecordingConflict(
                    "Cannot replace an existing result with an error"
                )
            call.phase, call.failure = "failed", failure
            call.not_dispatched = not_dispatched and not call.authorized

        self._change(scope, invocation.generation, change)

    def deliver(self, scope: Scope, invocation: Invocation) -> int:
        def change(recording: Recording) -> int:
            entry = recording.entries[invocation.ordinal]
            if entry.call_id != invocation.request.id:
                raise RecordingConflict("Delivery request differs")
            if recording.calls[entry.call_id].phase not in ("issued", "failed"):
                raise RecordingConflict("Result is not durable yet")
            if entry.delivery is None:
                entry.delivery = sum(e.delivery is not None for e in recording.entries)
            return entry.delivery

        return self._change(scope, invocation.generation, change)

    def release(self, scope: Scope, invocation: Invocation) -> None:
        def change(recording: Recording) -> None:
            call = recording.calls[invocation.request.id]
            if call.phase in ("reserved", "assigned"):
                call.phase, call.failure = "failed", "ReleasedBeforeCapture"
                call.not_dispatched = True

        self._change(scope, invocation.generation, change)

    def finish(
        self, scope: Scope, generation: Generation, count: int, output: str | None
    ) -> None:
        digest = hashlib.sha256((output or "").encode()).hexdigest()
        self.finish_digest(scope, generation, count, digest, output is None)

    def finish_digest(
        self,
        scope: Scope,
        generation: Generation,
        count: int,
        digest: str,
        is_none: bool,
    ) -> None:

        def change(recording: Recording) -> None:
            if count != len(recording.entries) or any(
                e.delivery is None for e in recording.entries
            ):
                raise RecordingConflict("Generation has unconsumed calls")
            if recording.finished and (
                recording.output_sha256 != digest or recording.output_is_none != is_none
            ):
                raise RecordingConflict("Generation output differs")
            recording.finished, recording.output_sha256 = True, digest
            recording.output_is_none = is_none

        self._change(scope, generation, change)

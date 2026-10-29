"""Durable allocation-scoped model calls and their capture attempts."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from witness_client.calls import RequestKey

from .models import Scope, identifier, json_bytes
from .state import StateConflict, StateStore

T = TypeVar("T")


class RecordingConflict(RuntimeError):
    pass


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request: RequestKey
    exchange_id: str
    deadline: int | None = None
    phase: Literal["reserved", "assigned", "capturing", "issued", "failed"] = "reserved"
    start_id: str | None = None
    pod_id: str | None = None
    capture_id: str | None = None
    authorized: bool = False
    not_dispatched: bool = False
    manifest_sha256: str | None = None
    failure: str | None = None


class CallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[2] = 2
    revision: str
    scope_key: str
    call: Call
    attempts: dict[str, Call] = Field(default_factory=dict, exclude=True)


class CallRecords:
    def __init__(self, store: StateStore, *, exchange_seconds: int = 1800) -> None:
        if not 60 <= exchange_seconds <= 86400:
            raise ValueError("Invalid exchange deadline")
        self.store, self.exchange_seconds = store, exchange_seconds

    @staticmethod
    def key(scope: Scope, call_id: str) -> str:
        if len(call_id) != 64 or any(c not in "0123456789abcdef" for c in call_id):
            raise ValueError("Invalid call ID")
        return f"pod-calls/v2/{scope.deployment}/{scope.round}/{scope.hotkey}/{call_id}.json"

    def load(self, scope: Scope, call_id: str) -> CallRecord | None:
        snapshot = self.store.load(self.key(scope, call_id))
        if snapshot is None:
            return None
        record = CallRecord.model_validate_json(snapshot.data)
        if record.scope_key != scope.key or record.call.request.id != call_id:
            raise RecordingConflict("Call identity differs")
        return record

    def _change(self, scope: Scope, request: RequestKey, change: Callable[[CallRecord], T]) -> T:
        key = self.key(scope, request.id)
        for _ in range(16):
            snapshot = self.store.load(key)
            record = (
                CallRecord.model_validate_json(snapshot.data)
                if snapshot is not None
                else CallRecord(
                    revision=str(uuid.uuid4()),
                    scope_key=scope.key,
                    call=Call(
                        request=request,
                        exchange_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope.key}/{request.id}")),
                    ),
                )
            )
            if record.scope_key != scope.key or record.call.request != request:
                raise RecordingConflict("Call identity differs")
            result = change(record)
            record.revision = str(uuid.uuid4())
            try:
                self.store.save(key, record.model_dump_json().encode(), None if snapshot is None else snapshot.etag)
                return result
            except StateConflict:
                continue
        raise StateConflict("Call is busy")

    def reserve(self, scope: Scope, request: RequestKey) -> Call:
        def change(record: CallRecord) -> Call:
            return record.call.model_copy(deep=True)

        call = self._change(scope, request, change)
        self._index(scope, call)
        return call

    def _index(self, scope: Scope, call: Call) -> None:
        key, data = self.index_key(scope, call.exchange_id), json_bytes({"call_id": call.request.id})
        try:
            self.store.save(key, data, None)
        except StateConflict:
            saved = self.store.load(key)
            if saved is None or saved.data != data:
                raise RecordingConflict("Exchange index conflicts") from None

    @staticmethod
    def index_key(scope: Scope, exchange_id: str) -> str:
        return f"pod-exchanges/v2/{scope.deployment}/{scope.round}/{scope.hotkey}/{identifier(exchange_id)}.json"

    def locate(self, scope: Scope, exchange_id: str) -> tuple[RequestKey, Call] | None:
        saved = self.store.load(self.index_key(scope, exchange_id))
        if saved is None:
            return None
        record = self.load(scope, json.loads(saved.data)["call_id"])
        if record is None:
            return None
        call = record.call if record.call.exchange_id == exchange_id else record.attempts.get(exchange_id)
        if call is None:
            return None
        return call.request, call

    def replaced(self, scope: Scope, exchange_id: str) -> bool:
        saved = self.store.load(self.index_key(scope, exchange_id))
        if saved is None:
            return False
        record = self.load(scope, json.loads(saved.data)["call_id"])
        return record is not None and record.call.exchange_id != exchange_id

    def retry(self, scope: Scope, request: RequestKey, exchange_id: str) -> Call:
        def change(record: CallRecord) -> Call:
            call = record.call
            if (
                call.exchange_id == exchange_id
                and call.phase == "failed"
            ):
                record.call = Call(
                    request=request, exchange_id=str(uuid.uuid4())
                )
            return record.call.model_copy(deep=True)

        call = self._change(scope, request, change)
        self._index(scope, call)
        return call

    def assign(
        self, scope: Scope, request: RequestKey, start_id: str, pod_id: str, *, allow_reassign: bool = False,
        expected_exchange_id: str | None = None,
    ) -> Call:
        def change(record: CallRecord) -> Call:
            call = record.call
            if (
                call.exchange_id == expected_exchange_id
                and call.phase == "failed"
            ):
                call = record.call = Call(
                    request=request, exchange_id=str(uuid.uuid4())
                )
            if call.phase == "reserved" or (
                call.phase == "assigned" and allow_reassign and (
                    call.start_id != start_id or call.pod_id != pod_id
                )
            ):
                call.deadline = None
                call.phase, call.start_id, call.pod_id = "assigned", start_id, pod_id
                call.capture_id = str(uuid.uuid4())
            return call.model_copy(deep=True)

        call = self._change(scope, request, change)
        self._index(scope, call)
        return call

    def claim(self, scope: Scope, request: RequestKey, pod_id: str, capture_id: str) -> bool:
        def change(record: CallRecord) -> bool:
            call = record.call
            if call.phase != "assigned" or call.pod_id != pod_id or call.capture_id != capture_id:
                return False
            call.phase = "capturing"
            return True

        return self._change(scope, request, change)

    def authorize(self, scope: Scope, request: RequestKey, pod_id: str, *, exchange_id: str) -> int:
        def change(record: CallRecord) -> int:
            call = record.call
            if call.exchange_id != exchange_id or call.phase != "capturing" or call.pod_id != pod_id or call.authorized:
                raise RecordingConflict("Capture is no longer eligible for authorization")
            call.authorized = True
            call.deadline = int(time.time()) + self.exchange_seconds
            return call.deadline

        return self._change(scope, request, change)

    def issued(self, scope: Scope, request: RequestKey, pod_id: str, manifest: bytes, *, exchange_id: str) -> None:
        digest = hashlib.sha256(manifest).hexdigest()

        def change(record: CallRecord) -> None:
            call = record.call
            if call.exchange_id != exchange_id:
                raise RecordingConflict("Capture attempt was superseded")
            if call.phase == "issued" and call.manifest_sha256 == digest:
                return
            if call.phase != "capturing" or not call.authorized or call.pod_id != pod_id:
                raise RecordingConflict("Capture cannot issue this result")
            call.phase, call.manifest_sha256 = "issued", digest

        self._change(scope, request, change)

    def failed(
        self,
        scope: Scope,
        request: RequestKey,
        pod_id: str,
        failure: str,
        *,
        exchange_id: str,
        not_dispatched: bool = False,
        manifest: bytes | None = None,
    ) -> None:
        def change(record: CallRecord) -> None:
            if record.call.exchange_id != exchange_id:
                return
            call = record.call
            if call.phase == "failed" and call.failure == failure:
                if call.pod_id == pod_id and call.manifest_sha256 is None and manifest is not None:
                    call.manifest_sha256 = hashlib.sha256(manifest).hexdigest()
                return
            if call.phase != "capturing" or call.pod_id != pod_id:
                raise RecordingConflict("Cannot replace an existing result with an error")
            call.phase, call.failure = "failed", failure
            call.not_dispatched = not_dispatched and not call.authorized
            if manifest is not None:
                call.manifest_sha256 = hashlib.sha256(manifest).hexdigest()

        self._change(scope, request, change)

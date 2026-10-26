"""Admission from RunPod inventory, including pending and duplicate real Pods."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .models import Pod, Scope, identifier
from .state import MAX_JOURNAL_BYTES, StateConflict, StateStore

T = TypeVar("T")


class CapacityReached(RuntimeError):
    pass


class CreateDeferred(RuntimeError):
    def __init__(self, retry_at: float) -> None:
        super().__init__("Pod creation must wait")
        self.retry_at = retry_at


class Admission:
    def __init__(self, *, max_pods: int) -> None:
        self.max_pods = max_pods
        if not 1 <= max_pods <= 1000:
            raise ValueError("Invalid fleet admission bounds")

    def check(self, inventory: list[Pod], deployment: str, *, pending: int = 0) -> None:
        pods = [
            p
            for p in inventory
            if p.scope() is not None
            and p.markers["WITNESS_DEPLOYMENT"] == deployment
            and p.markers["WITNESS_ROUND"] != "gateway"
        ]
        if len(pods) + pending >= self.max_pods:
            raise CapacityReached("Witness fleet capacity reached")

    def excess(self, inventory: list[Pod], deployment: str) -> list[Pod]:
        """Drain resources that appeared late beyond current provider headroom."""
        pods = [
            p
            for p in inventory
            if p.scope() is not None
            and p.markers["WITNESS_DEPLOYMENT"] == deployment
            and p.markers["WITNESS_ROUND"] != "gateway"
        ]
        count = len(pods)
        victims: list[Pod] = []
        for pod in sorted(
            pods, key=lambda p: (p.lifecycle == "active", -p.epoch_ns, p.id)
        ):
            if count <= self.max_pods:
                break
            victims.append(pod)
            count -= 1
        return victims


class PendingStart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    round: str
    start_id: str
    replaces: str | None = None
    sequence: int = Field(ge=0)
    eligible_at: int = Field(ge=0)
    submitted: bool = False


class CreateAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    round: str
    start_id: str
    at: int = Field(ge=0)
    rejected: bool = False
    submitted: bool = True


class QueueState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    deployment: str
    revision: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sequence: int = Field(default=0, ge=0)
    last_create_at: int | None = None
    pending: dict[str, PendingStart] = Field(default_factory=dict)
    attempts: dict[str, CreateAttempt] = Field(default_factory=dict)
    history: dict[str, dict[str, CreateAttempt]] = Field(default_factory=dict)


class StartQueue:
    """One durable FIFO intent per hotkey, with idempotent create attempts."""

    def __init__(self, store: StateStore, deployment: str, *, interval: float = 10, capacity: int = 1024,
                 clock: Callable[[], float] = time.time) -> None:
        if interval < 0 or not 1 <= capacity <= 4096:
            raise ValueError("Invalid allocation queue bounds")
        self.store, self.deployment = store, deployment
        self.interval, self.capacity, self.clock = interval, capacity, clock
        self.key = f"pod-admission/v1/{deployment}.json"
        self._state = QueueState(deployment=deployment)
        self._read_lock, self._write_lock = threading.Lock(), threading.Lock()

    def _load(self) -> tuple[QueueState, str | None]:
        saved = self.store.load(self.key)
        state = QueueState(deployment=self.deployment) if saved is None else QueueState.model_validate_json(saved.data)
        if state.deployment != self.deployment:
            raise StateConflict("Allocation queue belongs to another deployment")
        for hotkey, entry in state.pending.items():
            Scope(self.deployment, entry.round, hotkey)
            identifier(entry.start_id)
            if entry.replaces is not None:
                identifier(entry.replaces)
            state.pending[hotkey] = entry.model_copy(update={"eligible_at": min(entry.eligible_at, int(self.clock()))})
        for hotkey, attempt in state.attempts.items():
            Scope(self.deployment, attempt.round, hotkey)
            identifier(attempt.start_id)
            state.history.setdefault(hotkey, {}).setdefault(attempt.start_id, attempt)
        for hotkey, attempts in state.history.items():
            for start_id, attempt in attempts.items():
                Scope(self.deployment, attempt.round, hotkey)
                identifier(start_id)
                identifier(attempt.start_id)
        return state, None if saved is None else saved.etag

    def load(self) -> None:
        with self._write_lock:
            state, _ = self._load()
            with self._read_lock:
                self._state = state

    def _change(self, change: Callable[[QueueState], T]) -> T:
        with self._write_lock:
            state, etag = self._load()
            result = change(state)
            state.revision = str(uuid.uuid4())
            data = state.model_dump_json().encode()
            if len(data) > MAX_JOURNAL_BYTES:
                raise CapacityReached("Allocation journal is full")
            self.store.save(self.key, data, etag)
            with self._read_lock:
                self._state = state
            return result

    def entries(self) -> list[tuple[Scope, PendingStart]]:
        with self._read_lock:
            return sorted(
                ((Scope(self.deployment, entry.round, hotkey), entry) for hotkey, entry in self._state.pending.items()),
                key=lambda item: item[1].sequence,
            )

    def pending(self, scope: Scope) -> PendingStart | None:
        with self._read_lock:
            entry = self._state.pending.get(scope.hotkey)
            return entry if entry is not None and entry.round == scope.round else None

    def attempt(self, scope: Scope, start_id: str | None = None) -> CreateAttempt | None:
        with self._read_lock:
            attempt = (self._state.attempts.get(scope.hotkey) if start_id is None else
                       self._state.history.get(scope.hotkey, {}).get(start_id))
            if attempt is not None and attempt.round != scope.round:
                return None
            if attempt is not None and start_id is not None:
                attempt = self._state.history.get(scope.hotkey, {}).get(attempt.start_id, attempt)
            return attempt if attempt is not None and attempt.round == scope.round else None

    def unresolved(self) -> set[tuple[Scope, str]]:
        return {(scope, entry.start_id) for scope, entry in self.entries() if entry.submitted}

    def enqueue(self, scope: Scope, start_id: str, replaces: str | None) -> PendingStart:
        identifier(start_id)
        if replaces is not None:
            identifier(replaces)
        cached = self.pending(scope)
        if cached is not None and (cached.start_id == start_id or self.attempt(scope, start_id) is not None):
            return cached

        def change(state: QueueState) -> PendingStart:
            current = state.pending.get(scope.hotkey)
            if current is not None:
                if current.round != scope.round:
                    raise CreateDeferred(self.clock() + 60)
                previous = state.history.get(scope.hotkey, {}).get(start_id)
                if previous is None or previous.round != scope.round:
                    state.history.setdefault(scope.hotkey, {})[start_id] = CreateAttempt(
                        round=scope.round, start_id=current.start_id, at=math.ceil(self.clock()), submitted=False,
                    )
                return current
            if len(state.pending) >= self.capacity:
                raise CapacityReached("Allocation queue is full")
            previous = state.history.get(scope.hotkey, {}).get(start_id)
            if previous is not None and previous.round == scope.round:
                raise StateConflict("Connection has already been attempted")
            state.sequence += 1
            entry = PendingStart(
                round=scope.round, start_id=start_id, replaces=replaces, sequence=state.sequence,
                eligible_at=int(self.clock()),
            )
            state.pending[scope.hotkey] = entry
            state.history.setdefault(scope.hotkey, {})[start_id] = CreateAttempt(
                round=scope.round, start_id=start_id, at=math.ceil(self.clock()), submitted=False,
            )
            return entry

        return self._change(change)

    def authorize(self, scope: Scope, start_id: str) -> None:
        def change(state: QueueState) -> None:
            entry = state.pending.get(scope.hotkey)
            now = self.clock()
            if entry is None or (entry.round, entry.start_id) != (scope.round, start_id) or entry.submitted:
                raise CreateDeferred(now + 60)
            previous = state.history.get(scope.hotkey, {}).get(start_id)
            due = 0 if state.last_create_at is None or self.interval == 0 else state.last_create_at + self.interval
            if now < due:
                raise CreateDeferred(due)
            if previous is not None and previous.round == scope.round and previous.submitted:
                raise StateConflict("Connection has already been attempted")
            state.pending[scope.hotkey] = entry.model_copy(update={"submitted": True})
            state.attempts[scope.hotkey] = CreateAttempt(round=scope.round, start_id=start_id, at=math.ceil(now))
            state.history.setdefault(scope.hotkey, {})[start_id] = state.attempts[scope.hotkey]
            state.last_create_at = math.ceil(now)

        self._change(change)

    def complete(self, scope: Scope, start_id: str, *, rejected: bool = False) -> None:
        def change(state: QueueState) -> None:
            entry = state.pending.get(scope.hotkey)
            if entry is not None and (entry.round, entry.start_id) == (scope.round, start_id):
                del state.pending[scope.hotkey]
            previous = state.attempts.get(scope.hotkey)
            if rejected and previous is not None and (previous.round, previous.start_id) == (scope.round, start_id):
                state.attempts[scope.hotkey] = previous.model_copy(update={"rejected": True})
                state.history.setdefault(scope.hotkey, {})[start_id] = state.attempts[scope.hotkey]

        self._change(change)

    def cancel(self, scope: Scope, start_id: str) -> bool:
        def change(state: QueueState) -> bool:
            entry = state.pending.get(scope.hotkey)
            if entry is None:
                return True
            if (entry.round, entry.start_id) != (scope.round, start_id) or entry.submitted:
                return False
            del state.pending[scope.hotkey]
            return True

        return self._change(change)

    def discard_old(self, round_number: str) -> None:
        def change(state: QueueState) -> None:
            state.pending = {hotkey: entry for hotkey, entry in state.pending.items()
                             if entry.round == round_number or entry.submitted}
            state.attempts = {hotkey: attempt for hotkey, attempt in state.attempts.items()
                              if attempt.round == round_number}
            state.history = {hotkey: {start_id: attempt for start_id, attempt in attempts.items()
                                     if attempt.round == round_number}
                             for hotkey, attempts in state.history.items()}

        self._change(change)

"""RunPod owns Pod identity and lifecycle. Only create attempts are kept in memory."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from .admission import Admission
from .models import Pod, PodSpec, Scope, Session, identifier
from .runpod import CreateRejected, PodProvider, ProviderUnavailable


class AllocationConflict(RuntimeError):
    pass


class InventoryConflict(RuntimeError):
    pass


class WitnessControl(Protocol):
    def drain(self, pod: Pod) -> bool: ...


@dataclass
class Operation:
    session: Session
    epoch_ns: int
    since: float
    submitted: bool = False


class Allocator:
    """One gateway writes workers; the independent revivor writes gateways.

    The lock serializes create admission in this process. A restart discovers
    existing Pods directly. A lost POST may produce a duplicate after restart:
    both boot dormant and only a confirmed active winner can receive work.
    """

    def __init__(
        self,
        provider: PodProvider,
        control: WitnessControl,
        spec: PodSpec,
        *,
        admission: Admission | None = None,
        create_grace: float = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.provider, self.control, self.spec = provider, control, spec
        self.admission, self.create_grace, self.clock = admission, create_grace, clock
        self.lock = threading.RLock()
        self.operations: dict[Scope, Operation] = {}

    @staticmethod
    def owned(inventory: list[Pod], scope: Scope) -> list[Pod]:
        return [p for p in inventory if p.scope() == scope]

    @staticmethod
    def winner(pods: list[Pod]) -> Pod | None:
        if not pods:
            return None
        # Newer connection epochs supersede older connections. Within a
        # connection an already activated Pod wins over any late dormant copy.
        starts: dict[str, int] = {}
        for pod in pods:
            start = pod.markers["WITNESS_START_ID"]
            starts[start] = max(starts.get(start, 0), pod.epoch_ns)
        start = max(starts, key=lambda s: (starts[s], s))
        copies = [p for p in pods if p.markers["WITNESS_START_ID"] == start]
        active = [p for p in copies if p.lifecycle == "active"]
        viable = [p for p in copies if p.lifecycle == "pending"]
        return min(active or viable or copies, key=lambda p: p.id)

    def current(self, scope: Scope) -> Session | None:
        with self.lock:
            pod = self.winner(self.owned(self.provider.list(), scope))
            operation = self.operations.get(scope)
            if operation is not None and operation.session.phase in (
                "creating",
                "draining",
            ):
                if self.clock() - operation.since >= self.create_grace:
                    operation.session = replace(
                        operation.session, phase="lost", pod_id=None
                    )
                elif pod is None or operation.epoch_ns > pod.epoch_ns:
                    return operation.session
            return None if pod is None else pod.session()

    def status(self, scope: Scope, start_id: str) -> Session | None:
        identifier(start_id)
        with self.lock:
            pods = [
                p
                for p in self.owned(self.provider.list(), scope)
                if p.markers["WITNESS_START_ID"] == start_id
            ]
            pod = self.winner(pods)
            if pod is not None:
                return pod.session()
            operation = self.operations.get(scope)
            if operation is not None and operation.session.start_id == start_id:
                if (
                    operation.session.phase == "allocated"
                    or self.clock() - operation.since >= self.create_grace
                ):
                    return replace(operation.session, phase="lost", pod_id=None)
                return operation.session
            return None

    def selected(self, scope: Scope, start_id: str, pod_id: str) -> bool:
        with self.lock:
            pod = self.winner(self.owned(self.provider.list(), scope))
            return (
                pod is not None
                and pod.lifecycle == "active"
                and pod.status == "RUNNING"
                and pod.id == pod_id
                and pod.markers["WITNESS_START_ID"] == start_id
            )

    def _rename(self, pod: Pod, lifecycle: str) -> Pod:
        scope = pod.scope()
        if scope is None:
            raise InventoryConflict("Invalid Pod ownership")
        current = self.provider.get(pod.id)
        if (
            current is None
            or current.scope() != scope
            or current.image != pod.image
            or dict(current.markers) != dict(pod.markers)
        ):
            raise InventoryConflict("Pod identity changed")
        name = scope.name(pod.markers["WITNESS_START_ID"]) + "-" + lifecycle
        if current.name == name:
            return current
        if current.name != pod.name:
            raise InventoryConflict("Pod lifecycle changed")
        try:
            self.provider.rename(pod.id, name)
        except ProviderUnavailable:
            # PATCH replies are not lifecycle facts; confirm the exact resource.
            pass
        actual = self.provider.get(pod.id)
        if (
            actual is None
            or actual.name != name
            or actual.image != pod.image
            or dict(actual.markers) != dict(pod.markers)
        ):
            raise ProviderUnavailable("Pod lifecycle update is unconfirmed")
        return actual

    def retire(self, scope: Scope, start_id: str, pod_id: str) -> Session:
        with self.lock:
            pod = self.provider.get(pod_id)
            if pod is None:
                return Session(
                    start_id,
                    None,
                    "retired",
                    self.spec.fingerprint,
                    self.spec.image,
                    pod_id,
                )
            if pod.scope() != scope or pod.markers["WITNESS_START_ID"] != start_id:
                raise InventoryConflict("Cannot retire a different Pod")
            original = pod.session()
            if pod.lifecycle != "drained":
                pod = self._rename(pod, "draining")
                if pod.status not in ("EXITED", "ERROR") and not self.control.drain(pod):
                    return replace(original, phase="retiring")
                pod = self._rename(pod, "drained")
            try:
                self.provider.delete(pod.id)
            except ProviderUnavailable:
                pass
            if self.provider.get(pod.id) is not None:
                return replace(original, phase="retiring")
            return replace(original, phase="retired")

    def _reconcile(self, scope: Scope, inventory: list[Pod]) -> Session | None:
        pods = self.owned(inventory, scope)
        winner = self.winner(pods)
        if winner is None:
            return None
        blocked = False
        for pod in pods:
            if pod.id != winner.id:
                retired = self.retire(scope, pod.markers["WITNESS_START_ID"], pod.id)
                # A new connection waits for previous captures. Dormant duplicate
                # deletion never interrupts an already active winner.
                if (
                    pod.markers["WITNESS_START_ID"]
                    != winner.markers["WITNESS_START_ID"]
                ):
                    blocked |= retired.phase != "retired"
        if winner.lifecycle in ("draining", "drained") or winner.status in ("EXITED", "ERROR"):
            return self.retire(scope, winner.markers["WITNESS_START_ID"], winner.id)
        if winner.lifecycle == "pending":
            if blocked:
                return winner.session()
            if (
                winner.image != self.spec.image
                or winner.markers["WITNESS_POD_SPEC"] != self.spec.fingerprint
            ):
                return self.retire(scope, winner.markers["WITNESS_START_ID"], winner.id)
            winner = self._rename(winner, "active")
        return winner.session()

    def reconcile(self, scope: Scope) -> Session | None:
        with self.lock:
            operation = self.operations.get(scope)
            if (
                operation is not None
                and operation.session.phase == "draining"
                and not operation.submitted
            ):
                return self._progress(scope, operation)
            actual = self._reconcile(scope, self.provider.list(fresh=True))
            if actual is not None:
                if (
                    operation is not None
                    and actual.start_id == operation.session.start_id
                ):
                    operation.session = actual
                return actual
            return self.current(scope)

    def acquire(
        self, scope: Scope, start_id: str, *, replaces: str | None = None
    ) -> Session:
        identifier(start_id)
        if replaces is not None:
            identifier(replaces)
        with self.lock:
            inventory = self.provider.list(fresh=True)
            pods = self.owned(inventory, scope)
            same = [p for p in pods if p.markers["WITNESS_START_ID"] == start_id]
            if same:
                existing = self.winner(same)
                assert existing is not None
                session = existing.session()
                if (
                    session.replaces != replaces
                    or session.spec_hash != self.spec.fingerprint
                ):
                    raise AllocationConflict("Connection ID has different parameters")
                self._reconcile(scope, inventory)
                return self.status(scope, start_id) or replace(session, phase="retired")
            operation = self.operations.get(scope)
            if operation is not None and operation.session.start_id == start_id:
                if (
                    operation.session.replaces != replaces
                    or operation.session.spec_hash != self.spec.fingerprint
                ):
                    raise AllocationConflict("Connection ID has different parameters")
                if self.clock() - operation.since >= self.create_grace:
                    return replace(operation.session, phase="lost", pod_id=None)
                return self._progress(scope, operation)
            current = self.winner(pods)
            if current is not None and replaces != current.markers["WITNESS_START_ID"]:
                raise AllocationConflict("Replacement must name the current connection")
            if (
                operation is not None
                and operation.session.phase in ("draining", "creating")
                and self.clock() - operation.since < self.create_grace
            ):
                raise AllocationConflict("A create attempt is still unresolved")
            epoch = max(
                int(self.clock() * 1_000_000_000),
                max((p.epoch_ns for p in pods), default=0) + 1,
            )
            operation = Operation(
                Session(
                    start_id,
                    replaces,
                    "draining",
                    self.spec.fingerprint,
                    self.spec.image,
                ),
                epoch,
                self.clock(),
            )
            self.operations[scope] = operation
            return self._progress(scope, operation)

    def _progress(self, scope: Scope, operation: Operation) -> Session:
        inventory = self.provider.list(fresh=True)
        same = [
            p
            for p in self.owned(inventory, scope)
            if p.markers["WITNESS_START_ID"] == operation.session.start_id
        ]
        if same:
            actual = self._reconcile(scope, inventory)
            assert actual is not None
            operation.session = actual
            return actual
        if operation.submitted:
            return operation.session
        for pod in self.owned(inventory, scope):
            if (
                self.retire(scope, pod.markers["WITNESS_START_ID"], pod.id).phase
                != "retired"
            ):
                return operation.session
        inventory = self.provider.list(fresh=True)
        if self.admission is not None:
            visible = {p.markers.get("WITNESS_START_ID") for p in inventory}
            pending = sum(
                o.submitted
                and o.session.phase == "creating"
                and o.session.start_id not in visible
                and self.clock() - o.since < self.create_grace
                for s, o in self.operations.items()
                if s.deployment == scope.deployment
            )
            self.admission.check(inventory, scope.deployment, pending=pending)
        operation.submitted = True
        operation.session = replace(operation.session, phase="creating")
        try:
            pod = self.provider.create(
                self.spec.body(
                    scope,
                    operation.session.start_id,
                    epoch_ns=operation.epoch_ns,
                    replaces=operation.session.replaces,
                )
            )
        except CreateRejected as error:
            operation.session = replace(
                operation.session, phase="rejected", rejection=error.status
            )
            return operation.session
        except ProviderUnavailable:
            return operation.session
        if not pod.matches(scope, operation.session):
            raise InventoryConflict("Create returned a different Pod")
        actual = self._reconcile(scope, self.provider.list(fresh=True))
        if actual is not None:
            operation.session = actual
        return operation.session

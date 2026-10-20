"""Acquire/replace and recover allocations without replaying an uncertain create.

This is an internal control-plane library, not an authenticated public endpoint.
Only a journal transition from draining to creating grants permission to POST.
Readers of an existing creating record can only reconcile inventory.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Protocol

from .admission import Admission
from .models import Journal, Pod, PodSpec, Scope, Session, identifier
from .runpod import CreateRejected, PodProvider, ProviderUnavailable
from .state import StateStore


class AllocationConflict(RuntimeError):
    pass


class InventoryConflict(RuntimeError):
    """Owned inventory needs reconciliation; no Pod is selected or created."""


class WitnessControl(Protocol):
    def drain(self, pod: Pod) -> bool:
        """Idempotently fence new work; True only after capture and R2 uploads finish.

        Unreachable is NOT drained. The journal has already revoked selection.
        The eventual Pod listener must authenticate this operator-only request.
        """
        ...


class Allocator:
    def __init__(
        self,
        store: StateStore,
        provider: PodProvider,
        control: WitnessControl,
        spec: PodSpec,
        *,
        max_starts: int = 1024,
        admission: Admission | None = None,
    ) -> None:
        if not 1 <= max_starts <= 1024:
            raise ValueError("Invalid allocation history limit")
        self.store = store
        self.provider = provider
        self.control = control
        self.spec = spec
        self.max_starts = max_starts
        self.admission = admission

    def acquire(
        self, scope: Scope, start_id: str, *, replaces: str | None = None
    ) -> Session:
        """A deliberate new connection uses a new UUID and the observed current ID.

        Transport retries reuse both arguments. Replaying an older UUID never
        changes the current session, including after a process restart.
        """
        identifier(start_id)
        if replaces is not None:
            identifier(replaces)
        loaded = self._load(scope)
        if loaded is None:
            if replaces is not None:
                raise AllocationConflict("No current session to replace")
            journal = Journal(scope, start_id, (self._new(start_id, None),))
            journal, etag = self._save(journal, None)
        else:
            journal, etag = loaded
            previous = next(
                (s for s in journal.sessions if s.start_id == start_id), None
            )
            if previous is not None:
                if (
                    previous.replaces != replaces
                    or previous.spec_hash != self.spec.fingerprint
                ):
                    raise AllocationConflict(
                        "Session ID was used with different parameters"
                    )
                if start_id != journal.current:
                    return previous
                if previous.phase in ("allocated", "rejected", "lost", "retired"):
                    return previous
                return self._advance(journal, etag)
            current = journal.session(journal.current)
            if replaces != current.start_id:
                raise AllocationConflict("Replacement must name the current session")
            if current.phase in ("draining", "creating", "retiring"):
                raise AllocationConflict(
                    "Current provisioning must be reconciled first"
                )
            if len(journal.sessions) >= self.max_starts:
                raise AllocationConflict("Round allocation history limit reached")
            journal = replace(
                journal,
                current=start_id,
                sessions=(
                    *journal.sessions[:-1],
                    replace(current, phase="retired"),
                    self._new(start_id, replaces),
                ),
            )
            # Revoke the old selection durably BEFORE asking it to drain.
            journal, etag = self._save(journal, etag)
        return self._advance(journal, etag)

    def status(self, scope: Scope, start_id: str) -> Session | None:
        identifier(start_id)
        loaded = self._load(scope)
        if loaded is None:
            return None
        return next((s for s in loaded[0].sessions if s.start_id == start_id), None)

    def selected(self, scope: Scope, start_id: str, pod_id: str) -> bool:
        """Selection only; this does not authenticate or establish Pod readiness."""
        loaded = self._load(scope)
        if loaded is None or loaded[0].current != start_id:
            return False
        session = loaded[0].session(start_id)
        return session.phase == "allocated" and session.pod_id == pod_id

    def reconcile(self, scope: Scope) -> Session | None:
        loaded = self._load(scope)
        return None if loaded is None else self._advance(*loaded)

    def current(self, scope: Scope) -> Session | None:
        loaded = self._load(scope)
        return None if loaded is None else loaded[0].session(loaded[0].current)

    def retire(self, scope: Scope, start_id: str, pod_id: str) -> Session:
        loaded = self._load(scope)
        if loaded is None or loaded[0].current != start_id:
            raise AllocationConflict("Cannot retire a different current session")
        journal, etag = loaded
        current = journal.session(start_id)
        if current.pod_id != pod_id or current.phase not in (
            "allocated",
            "retiring",
            "retired",
        ):
            raise AllocationConflict("Cannot retire this Pod")
        if current.phase == "allocated":
            current = replace(current, phase="retiring")
            journal, etag = self._save(
                replace(journal, sessions=(*journal.sessions[:-1], current)), etag
            )
        return self._advance(journal, etag)

    def _new(self, start_id: str, replaces: str | None) -> Session:
        return Session(
            start_id, replaces, "draining", self.spec.fingerprint, self.spec.image
        )

    def _load(self, scope: Scope) -> tuple[Journal, str] | None:
        snapshot = self.store.load(scope.key)
        return (
            None
            if snapshot is None
            else (Journal.decode(snapshot.data, scope), snapshot.etag)
        )

    def _save(self, journal: Journal, expected: str | None) -> tuple[Journal, str]:
        journal = replace(journal, revision=str(uuid.uuid4()))
        saved = self.store.save(journal.scope.key, journal.encode(), expected)
        return journal, saved.etag

    def _update(self, journal: Journal, session: Session, etag: str) -> Session:
        self._save(replace(journal, sessions=(*journal.sessions[:-1], session)), etag)
        return session

    def _owned(self, scope: Scope) -> list[Pod]:
        return [pod for pod in self.provider.list() if pod.in_scope(scope)]

    @staticmethod
    def _check(pod: Pod, scope: Scope, session: Session) -> None:
        if not pod.matches(scope, session) or (
            session.pod_id is not None and pod.id != session.pod_id
        ):
            raise InventoryConflict("Pod identity does not match its allocation")

    def _advance(self, journal: Journal, etag: str) -> Session:
        current = journal.session(journal.current)
        scope = journal.scope
        if current.phase == "retiring":
            assert current.pod_id is not None
            pod = self.provider.get(current.pod_id)
            if pod is not None:
                self._check(pod, scope, current)
                if not self.control.drain(pod):
                    return current
                try:
                    self.provider.delete(pod.id)
                except ProviderUnavailable:
                    pass
                if self.provider.get(pod.id) is not None:
                    return current
            if self.admission is not None:
                self.admission.release(scope, current.start_id)
            return self._update(journal, replace(current, phase="retired"), etag)
        if current.phase == "allocated":
            assert current.pod_id is not None
            pod = self.provider.get(current.pod_id)
            if pod is None:
                return self._update(journal, replace(current, phase="lost"), etag)
            self._check(pod, scope, current)
            return current
        if current.phase == "creating":
            # Even an empty list is not proof that an earlier POST cannot finish.
            pods = self._owned(scope)
            if not pods:
                return current
            if len(pods) != 1:
                raise InventoryConflict("Multiple owned Pods need reconciliation")
            pod = pods[0]
            self._check(pod, scope, current)
            return self._update(
                journal, replace(current, phase="allocated", pod_id=pod.id), etag
            )
        if current.phase != "draining":
            return current
        if current.spec_hash != self.spec.fingerprint:
            raise AllocationConflict("Provisioning configuration changed")
        if current.replaces is not None:
            previous = journal.session(current.replaces)
            if previous.pod_id is not None:
                pod = self.provider.get(previous.pod_id)
                if pod is not None:
                    self._check(pod, scope, previous)
                    if not self.control.drain(pod):
                        return current
                    try:
                        self.provider.delete(pod.id)
                    except ProviderUnavailable:
                        # A lost DELETE reply is resolved by GET, never assumed.
                        pass
                    if self.provider.get(pod.id) is not None:
                        return current
            if self.admission is not None:
                self.admission.release(scope, previous.start_id)
        # Block on stray owned Pods, including late creates and unknown sessions.
        # Never delete a resource merely because its name has our prefix.
        if self._owned(scope):
            raise InventoryConflict(
                "Existing owned Pods must be reconciled before creation"
            )
        if self.admission is not None:
            self.admission.reserve(scope, current.start_id)
        creating = replace(current, phase="creating")
        journal, etag = self._save(
            replace(journal, sessions=(*journal.sessions[:-1], creating)), etag
        )
        # Only the caller that confirmed THIS transition may submit the POST.
        try:
            pod = self.provider.create(self.spec.body(scope, current.start_id))
        except CreateRejected as error:
            if error.status is None:
                raise ValueError(
                    "Provider rejection must include an HTTP status"
                ) from None
            if self.admission is not None:
                self.admission.release(scope, current.start_id)
            return self._update(
                journal,
                replace(creating, phase="rejected", rejection=error.status),
                etag,
            )
        except ProviderUnavailable:
            return creating
        self._check(pod, scope, creating)
        return self._update(
            journal, replace(creating, phase="allocated", pod_id=pod.id), etag
        )

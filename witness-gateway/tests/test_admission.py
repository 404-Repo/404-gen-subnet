from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from support import MemoryState, Provider
from test_lifecycle import FIRST, SCOPE, SECOND, SPEC, Drain

from pod_gateway.admission import Admission, CapacityReached, CreateDeferred, StartQueue
from pod_gateway.lifecycle import Allocator
from pod_gateway.models import Pod
from pod_gateway.runpod import CreateUnknown
from pod_gateway.state import Snapshot, StateConflict, WriteUncertain


def test_capacity_uses_actual_inventory_and_frees_when_the_pod_disappears() -> None:
    provider = Provider()
    policy = Admission(max_pods=1)
    old = Allocator(provider, Drain(), SPEC, admission=policy).acquire(SCOPE, FIRST)
    other = replace(SCOPE, hotkey="6" * 48)
    restarted = Allocator(provider, Drain(), SPEC, admission=policy)
    with pytest.raises(CapacityReached):
        restarted.acquire(other, SECOND)
    provider.pods.pop(old.pod_id or "")
    assert restarted.acquire(other, SECOND).phase == "allocated"


def test_duplicate_pending_and_exited_pods_count_until_confirmed_deleted() -> None:
    provider = Provider()
    first = provider.create(SPEC.body(SCOPE, FIRST))
    second = provider.create(SPEC.body(SCOPE, FIRST))
    provider.pods[second.id] = replace(second, status="EXITED")
    policy = Admission(max_pods=2)
    with pytest.raises(CapacityReached):
        policy.check(provider.list(), SCOPE.deployment)
    provider.delete(first.id)
    policy.check(provider.list(), SCOPE.deployment)


def test_another_miners_matching_start_id_does_not_hide_a_pending_create() -> None:
    class DelayedCreate(Provider):
        delayed: dict[str, object] | None = None
        delay_next = False

        def create(self, body: dict[str, object]) -> Pod:
            if self.delay_next:
                self.delay_next = False
                self.delayed = body
                raise CreateUnknown("Create is not visible yet")
            return super().create(body)

    provider = DelayedCreate()
    allocator = Allocator(provider, Drain(), SPEC, admission=Admission(max_pods=2))
    other = replace(SCOPE, hotkey="6" * 48)
    waiting = replace(SCOPE, hotkey="7" * 48)
    allocator.acquire(SCOPE, FIRST)
    provider.delay_next = True
    assert allocator.acquire(other, FIRST).phase == "creating"

    with pytest.raises(CapacityReached):
        allocator.acquire(waiting, SECOND)

    assert provider.delayed is not None
    provider.create(provider.delayed)
    recovered = allocator.reconcile(other)
    assert recovered is not None and recovered.phase == "allocated"
    assert len(provider.pods) == 2


def test_concurrent_miners_share_serialized_provider_capacity_admission() -> None:
    provider = Provider()
    allocator = Allocator(
        provider,
        Drain(),
        SPEC,
        admission=Admission(max_pods=1),
    )

    def connect(character: str) -> bool:
        try:
            allocator.acquire(replace(SCOPE, hotkey=character * 48), FIRST)
            return True
        except CapacityReached:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(connect, "ABCDEFGH"))
    assert sum(accepted) == 1 and len(provider.pods) == 1


def test_retries_with_fresh_ids_keep_one_fifo_allocation() -> None:
    queue = StartQueue(MemoryState(), SCOPE.deployment, interval=0)
    provider = Provider()
    allocator = Allocator(provider, Drain(), SPEC)
    allocator.before_create = queue.authorize
    other = replace(SCOPE, hotkey="6" * 48)
    first = queue.enqueue(SCOPE, FIRST, None)
    queue.enqueue(other, SECOND, None)
    for _ in range(20):
        assert queue.enqueue(SCOPE, SECOND, FIRST) == first
    for scope, entry in queue.entries():
        assert allocator.acquire(scope, entry.start_id).phase == "allocated"
        queue.complete(scope, entry.start_id)
    assert [pod.scope() for pod in provider.pods.values()] == [SCOPE, other]
    assert provider.creates == 2


def test_fresh_allocations_share_only_global_pacing_across_restart_and_round_change() -> None:
    state, now = MemoryState(), [1000.0]
    queue = StartQueue(state, SCOPE.deployment, clock=lambda: now[0])
    queue.enqueue(SCOPE, FIRST, None)
    queue.authorize(SCOPE, FIRST)
    queue.complete(SCOPE, FIRST)
    now[0] += 1
    restarted = StartQueue(state, SCOPE.deployment, clock=lambda: now[0])
    restarted.load()
    next_round = replace(SCOPE, round="test-1")
    restarted.enqueue(next_round, SECOND, FIRST)
    with pytest.raises(CreateDeferred) as waiting:
        restarted.authorize(next_round, SECOND)
    assert waiting.value.retry_at == 1010
    now[0] = 1010
    restarted.authorize(next_round, SECOND)


def test_unknown_create_cannot_be_repeated_after_restart_or_elapsed_time() -> None:
    class Unknown(Provider):
        def create(self, body: dict[str, object]) -> Pod:
            self.creates += 1
            raise CreateUnknown("Create result is unknown")

    state, now, provider = MemoryState(), [1000.0], Unknown()
    queue = StartQueue(state, SCOPE.deployment, clock=lambda: now[0])
    queue.enqueue(SCOPE, FIRST, None)
    allocator = Allocator(provider, Drain(), SPEC)
    allocator.before_create = queue.authorize
    assert allocator.acquire(SCOPE, FIRST).phase == "creating"
    now[0] = 5000
    restarted = StartQueue(state, SCOPE.deployment, clock=lambda: now[0])
    restarted.load()
    assert restarted.enqueue(SCOPE, SECOND, FIRST).start_id == FIRST
    allocator = Allocator(provider, Drain(), SPEC)
    allocator.before_create = restarted.authorize
    with pytest.raises(CreateDeferred):
        allocator.acquire(SCOPE, FIRST)
    assert provider.creates == 1


def test_older_completed_requests_cannot_create_again_after_restart() -> None:
    state = MemoryState()
    queue = StartQueue(state, SCOPE.deployment, interval=0)
    for start_id in (FIRST, SECOND):
        queue.enqueue(SCOPE, start_id, None)
        queue.authorize(SCOPE, start_id)
        queue.complete(SCOPE, start_id)
    restarted = StartQueue(state, SCOPE.deployment, interval=0)
    restarted.load()
    assert restarted.attempt(SCOPE, FIRST) is not None
    with pytest.raises(StateConflict):
        restarted.enqueue(SCOPE, FIRST, None)


def test_a_request_joined_to_a_pending_allocation_cannot_create_after_completion() -> None:
    state = MemoryState()
    queue = StartQueue(state, SCOPE.deployment, interval=0)
    first = queue.enqueue(SCOPE, FIRST, None)
    assert queue.enqueue(SCOPE, SECOND, None) == first
    queue.authorize(SCOPE, FIRST)
    queue.complete(SCOPE, FIRST)
    restarted = StartQueue(state, SCOPE.deployment, interval=0)
    restarted.load()
    attempt = restarted.attempt(SCOPE, SECOND)
    assert attempt is not None and attempt.start_id == FIRST and attempt.submitted
    with pytest.raises(StateConflict):
        restarted.enqueue(SCOPE, SECOND, None)


def test_restored_unknown_create_still_occupies_capacity() -> None:
    state = MemoryState()
    queue = StartQueue(state, SCOPE.deployment)
    queue.enqueue(SCOPE, FIRST, None)
    queue.authorize(SCOPE, FIRST)
    restarted = StartQueue(state, SCOPE.deployment)
    restarted.load()
    provider = Provider()
    allocator = Allocator(provider, Drain(), SPEC, admission=Admission(max_pods=1))
    allocator.pending_creates = restarted.unresolved
    with pytest.raises(CapacityReached):
        allocator.acquire(replace(SCOPE, hotkey="6" * 48), SECOND)
    assert not provider.pods


def test_an_uncertain_admission_write_never_submits_a_create() -> None:
    class UncertainState(MemoryState):
        uncertain = False

        def save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
            snapshot = super().save(key, data, expected)
            if self.uncertain:
                raise WriteUncertain("Admission write may have completed")
            return snapshot

    state, provider = UncertainState(), Provider()
    queue = StartQueue(state, SCOPE.deployment)
    queue.enqueue(SCOPE, FIRST, None)
    allocator = Allocator(provider, Drain(), SPEC)
    allocator.before_create = queue.authorize
    state.uncertain = True
    with pytest.raises(WriteUncertain):
        allocator.acquire(SCOPE, FIRST)
    state.uncertain = False
    queue.load()
    assert queue.enqueue(SCOPE, SECOND, FIRST).start_id == FIRST
    with pytest.raises(CreateDeferred):
        allocator.acquire(SCOPE, FIRST)
    assert provider.creates == 0

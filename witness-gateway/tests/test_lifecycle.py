from __future__ import annotations

import json
from dataclasses import replace
from typing import Callable

import httpx
import pytest

from pod_gateway.lifecycle import AllocationConflict, Allocator, InventoryConflict
from pod_gateway.models import Journal, Pod, PodSpec, Scope, object_dict, string
from pod_gateway.runpod import CreateRejected, CreateUnknown, ProviderUnavailable, RunPodClient
from pod_gateway.state import Snapshot, StateConflict, WriteUncertain

SCOPE = Scope("competition", "test-0", "5" * 48)
SPEC = PodSpec("registry/witness@sha256:" + "a" * 64, env={"PRIVATE_KEY": "not-public"})
FIRST = "00000000-0000-4000-8000-000000000001"
SECOND = "00000000-0000-4000-8000-000000000002"
THIRD = "00000000-0000-4000-8000-000000000003"


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, Snapshot] = {}
        self.writes = 0
        self.before: Callable[[int], None] = lambda _: None
        self.after: Callable[[int], None] = lambda _: None

    def load(self, key: str) -> Snapshot | None:
        return self.objects.get(key)

    def save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
        self.writes += 1
        self.before(self.writes)
        current = self.objects.get(key)
        if (None if current is None else current.etag) != expected:
            raise StateConflict("Test conflict")
        saved = Snapshot(str(self.writes), data)
        self.objects[key] = saved
        self.after(self.writes)
        return saved


class FakeProvider:
    def __init__(self) -> None:
        self.pods: dict[str, Pod] = {}
        self.events: list[tuple[str, str]] = []
        self.create_mode = "ok"
        self.keep_after_delete = False
        self.lost_delete_reply = False
        self.during_create: Callable[[], None] = lambda: None

    @property
    def creates(self) -> int:
        return sum(event[0] == "create" for event in self.events)

    def list(self) -> list[Pod]:
        self.events.append(("list", ""))
        return list(self.pods.values())

    def get(self, pod_id: str) -> Pod | None:
        self.events.append(("get", pod_id))
        return self.pods.get(pod_id)

    def create(self, body: dict[str, object]) -> Pod:
        pod_id = f"pod-{self.creates + 1}"
        self.events.append(("create", pod_id))
        self.during_create()
        if self.create_mode == "reject":
            raise CreateRejected("quota", status=429)
        if self.create_mode == "unknown-empty":
            raise CreateUnknown("lost reply")
        pod = owned(pod_id, string(object_dict(body["env"])["WITNESS_START_ID"]))
        self.pods[pod_id] = pod
        if self.create_mode == "unknown-created":
            raise CreateUnknown("lost reply")
        return pod

    def delete(self, pod_id: str) -> None:
        self.events.append(("delete", pod_id))
        if not self.keep_after_delete:
            self.pods.pop(pod_id, None)
        if self.lost_delete_reply:
            raise ProviderUnavailable("lost delete reply")


class Drain:
    def __init__(self) -> None:
        self.ready = True
        self.calls: list[str] = []
        self.during_drain: Callable[[], None] = lambda: None

    def drain(self, pod: Pod) -> bool:
        self.calls.append(pod.id)
        self.during_drain()
        return self.ready


def owned(pod_id: str = "pod-1", start_id: str = FIRST, scope: Scope = SCOPE) -> Pod:
    return Pod(
        pod_id, scope.name(start_id), SPEC.image, "RUNNING",
        scope.markers(start_id, SPEC.fingerprint),
    )


@pytest.fixture
def setup() -> tuple[Allocator, MemoryStore, FakeProvider, Drain]:
    store, provider, drain = MemoryStore(), FakeProvider(), Drain()
    return Allocator(store, provider, drain, SPEC), store, provider, drain


def test_acquire_is_durable_idempotent_and_does_not_expose_secrets(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, store, provider, drain = setup
    first = allocator.acquire(SCOPE, FIRST)
    assert first.phase == "allocated" and first.pod_id == "pod-1"
    assert allocator.acquire(SCOPE, FIRST) == first
    restarted = Allocator(store, provider, drain, SPEC)
    assert restarted.acquire(SCOPE, FIRST) == first
    assert provider.creates == 1
    assert restarted.selected(SCOPE, FIRST, "pod-1")
    assert not restarted.selected(SCOPE, FIRST, "different")
    assert b"not-public" not in store.objects[SCOPE.key].data
    assert "not-public" not in repr(first)


def test_replacement_revokes_then_drains_and_confirms_deletion(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, _, provider, drain = setup
    allocator.acquire(SCOPE, FIRST)
    drain.ready = False
    drain.during_drain = lambda: assert_revoked(allocator)
    assert allocator.acquire(SCOPE, SECOND, replaces=FIRST).phase == "draining"
    assert provider.creates == 1
    assert not any(event[0] == "delete" for event in provider.events)
    drain.ready = True
    provider.keep_after_delete = True
    assert allocator.reconcile(SCOPE).phase == "draining"  # type: ignore[union-attr]
    assert provider.creates == 1
    provider.keep_after_delete = False
    assert allocator.reconcile(SCOPE).phase == "allocated"  # type: ignore[union-attr]
    assert provider.creates == 2
    second_create = provider.events.index(("create", "pod-2"))
    assert provider.events[second_create - 2:] == [
        ("get", "pod-1"), ("list", ""), ("create", "pod-2"),
    ]
    assert allocator.acquire(SCOPE, FIRST).phase == "retired"
    assert allocator.selected(SCOPE, SECOND, "pod-2")


def assert_revoked(allocator: Allocator) -> None:
    assert not allocator.selected(SCOPE, FIRST, "pod-1")


def test_lost_delete_reply_still_requires_confirmed_absence(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, _, provider, _ = setup
    allocator.acquire(SCOPE, FIRST)
    provider.lost_delete_reply = True
    provider.keep_after_delete = True
    assert allocator.acquire(SCOPE, SECOND, replaces=FIRST).phase == "draining"
    assert provider.creates == 1
    provider.keep_after_delete = False
    assert allocator.reconcile(SCOPE).phase == "allocated"  # type: ignore[union-attr]
    assert provider.creates == 2


@pytest.mark.parametrize("mode", ["unknown-empty", "unknown-created"])
def test_uncertain_create_is_never_repeated_even_after_restart(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain], mode: str,
) -> None:
    allocator, store, provider, drain = setup
    provider.create_mode = mode
    assert allocator.acquire(SCOPE, FIRST).phase == "creating"
    restarted = Allocator(store, provider, drain, SPEC)
    expected = "creating" if mode == "unknown-empty" else "allocated"
    assert restarted.acquire(SCOPE, FIRST).phase == expected
    assert restarted.reconcile(SCOPE).phase == expected  # type: ignore[union-attr]
    assert provider.creates == 1
    if mode == "unknown-empty":
        with pytest.raises(AllocationConflict):
            restarted.acquire(SCOPE, SECOND, replaces=FIRST)
        provider.pods["late-pod"] = owned("late-pod")
        assert restarted.reconcile(SCOPE).pod_id == "late-pod"  # type: ignore[union-attr]
        assert provider.creates == 1


def test_concurrent_retry_during_post_cannot_submit_or_replace(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, store, provider, drain = setup
    other = Allocator(store, provider, drain, SPEC)

    def concurrent() -> None:
        assert other.acquire(SCOPE, FIRST).phase == "creating"
        with pytest.raises(AllocationConflict):
            other.acquire(SCOPE, SECOND, replaces=FIRST)

    provider.during_create = concurrent
    assert allocator.acquire(SCOPE, FIRST).phase == "allocated"
    assert provider.creates == 1


@pytest.mark.parametrize("write", [1, 2])
def test_unconfirmed_journal_write_forbids_provider_side_effects(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain], write: int,
) -> None:
    allocator, _, provider, _ = setup
    store = allocator.store
    assert isinstance(store, MemoryStore)

    def fail(number: int) -> None:
        if number == write:
            raise WriteUncertain("test timeout")

    store.before = fail
    with pytest.raises(WriteUncertain):
        allocator.acquire(SCOPE, FIRST)
    assert provider.creates == 0
    assert not any(event[0] == "delete" for event in provider.events)


def test_crash_after_create_intent_does_not_replay_post(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, store, provider, drain = setup

    def crash(number: int) -> None:
        if number == 2:
            raise WriteUncertain("crash after committed intent")

    store.after = crash
    with pytest.raises(WriteUncertain):
        allocator.acquire(SCOPE, FIRST)
    store.after = lambda _: None
    assert Allocator(store, provider, drain, SPEC).acquire(SCOPE, FIRST).phase == "creating"
    assert provider.creates == 0


@pytest.mark.parametrize("committed", [False, True])
def test_crash_after_provider_create_recovers_the_same_pod(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain], committed: bool,
) -> None:
    allocator, store, provider, drain = setup

    def crash(number: int) -> None:
        if number == 3:
            raise WriteUncertain("lost allocation write")

    if committed:
        store.after = crash
    else:
        store.before = crash
    with pytest.raises(WriteUncertain):
        allocator.acquire(SCOPE, FIRST)
    store.before = store.after = lambda _: None
    session = Allocator(store, provider, drain, SPEC).acquire(SCOPE, FIRST)
    assert session.phase == "allocated" and session.pod_id == "pod-1"
    assert provider.creates == 1


def test_duplicate_or_stray_owned_pods_are_never_selected_or_deleted(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, _, provider, _ = setup
    provider.create_mode = "unknown-empty"
    allocator.acquire(SCOPE, FIRST)
    provider.pods = {"one": owned("one"), "two": owned("two")}
    with pytest.raises(InventoryConflict):
        allocator.reconcile(SCOPE)
    assert not allocator.selected(SCOPE, FIRST, "one")
    assert not allocator.selected(SCOPE, FIRST, "two")
    assert provider.creates == 1
    assert not any(event[0] == "delete" for event in provider.events)


def test_missing_journal_with_owned_inventory_fails_closed(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, _, provider, _ = setup
    provider.pods["stray"] = owned("stray", THIRD)
    with pytest.raises(InventoryConflict):
        allocator.acquire(SCOPE, FIRST)
    assert provider.creates == 0
    assert provider.pods["stray"].id == "stray"


def test_foreign_scope_and_name_prefix_are_never_deleted(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, _, provider, _ = setup
    foreign = owned("foreign", scope=replace(SCOPE, round="another-round"))
    provider.pods[foreign.id] = replace(foreign, name=SCOPE.name(FIRST))
    assert allocator.acquire(SCOPE, FIRST).phase == "allocated"
    allocator.acquire(SCOPE, SECOND, replaces=FIRST)
    assert "foreign" in provider.pods
    assert ("delete", "foreign") not in provider.events


def test_changed_pod_identity_blocks_deletion(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, _, provider, drain = setup
    allocator.acquire(SCOPE, FIRST)
    provider.pods["pod-1"] = replace(provider.pods["pod-1"], markers={})
    with pytest.raises(InventoryConflict):
        allocator.acquire(SCOPE, SECOND, replaces=FIRST)
    assert provider.creates == 1
    assert drain.calls == []
    assert not any(event[0] == "delete" for event in provider.events)


def test_rejected_request_is_terminal_for_that_id(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, _, provider, _ = setup
    provider.create_mode = "reject"
    session = allocator.acquire(SCOPE, FIRST)
    assert session.phase == "rejected" and session.rejection == 429
    assert allocator.acquire(SCOPE, FIRST) == session
    assert provider.creates == 1
    provider.create_mode = "ok"
    assert allocator.acquire(SCOPE, SECOND, replaces=FIRST).phase == "allocated"
    assert provider.creates == 2


def test_replacement_requires_exact_current_and_same_id_parameters(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, _, provider, _ = setup
    allocator.acquire(SCOPE, FIRST)
    for start_id, replaces in ((SECOND, None), (SECOND, THIRD), (FIRST, FIRST)):
        with pytest.raises(AllocationConflict):
            allocator.acquire(SCOPE, start_id, replaces=replaces)
    assert provider.creates == 1


def test_configuration_cannot_change_under_a_retry(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, store, provider, drain = setup
    allocator.acquire(SCOPE, FIRST)
    different = Allocator(store, provider, drain, replace(SPEC, vcpu_count=4))
    with pytest.raises(AllocationConflict):
        different.acquire(SCOPE, FIRST)
    assert provider.creates == 1


def test_lost_pod_does_not_trigger_automatic_recreation(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, _, provider, _ = setup
    allocator.acquire(SCOPE, FIRST)
    provider.pods.clear()
    assert allocator.reconcile(SCOPE).phase == "lost"  # type: ignore[union-attr]
    assert not allocator.selected(SCOPE, FIRST, "pod-1")
    assert allocator.acquire(SCOPE, FIRST).phase == "lost"
    assert provider.creates == 1


def test_r2_cas_conflict_cannot_grant_permission_to_post(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, store, provider, _ = setup

    def conflict(number: int) -> None:
        if number == 2:
            raise StateConflict("another controller won")

    store.before = conflict
    with pytest.raises(StateConflict):
        allocator.acquire(SCOPE, FIRST)
    assert provider.creates == 0
    assert allocator.status(SCOPE, FIRST).phase == "draining"  # type: ignore[union-attr]


def test_history_bound_does_not_discard_idempotency_records(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    _, store, provider, drain = setup
    allocator = Allocator(store, provider, drain, SPEC, max_starts=1)
    allocator.acquire(SCOPE, FIRST)
    with pytest.raises(AllocationConflict):
        allocator.acquire(SCOPE, SECOND, replaces=FIRST)
    assert allocator.acquire(SCOPE, FIRST).phase == "allocated"
    assert provider.creates == 1


def test_corrupt_or_wrong_scope_journal_cannot_touch_provider(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, store, provider, _ = setup
    allocator.acquire(SCOPE, FIRST)
    other = replace(SCOPE, round="different")
    store.objects[other.key] = store.objects[SCOPE.key]
    events = list(provider.events)
    with pytest.raises(ValueError):
        allocator.acquire(other, SECOND)
    store.objects[SCOPE.key] = Snapshot("bad", b"{}")
    with pytest.raises(ValueError):
        allocator.reconcile(SCOPE)
    assert provider.events == events


def test_journal_retains_ordered_replacement_history(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, store, _, _ = setup
    allocator.acquire(SCOPE, FIRST)
    allocator.acquire(SCOPE, SECOND, replaces=FIRST)
    allocator.acquire(SCOPE, THIRD, replaces=SECOND)
    journal = Journal.decode(store.objects[SCOPE.key].data, SCOPE)
    assert [s.phase for s in journal.sessions] == ["retired", "retired", "allocated"]
    assert allocator.acquire(SCOPE, SECOND, replaces=FIRST).phase == "retired"


def test_only_one_of_two_controllers_can_commit_permission_to_create(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, store, provider, drain = setup
    other = Allocator(store, provider, drain, SPEC)

    def interleave(number: int) -> None:
        if number == 2:
            assert other.reconcile(SCOPE).phase == "allocated"  # type: ignore[union-attr]

    store.before = interleave
    with pytest.raises(StateConflict):
        allocator.acquire(SCOPE, FIRST)
    assert allocator.acquire(SCOPE, FIRST).phase == "allocated"
    assert provider.creates == 1


def test_unreachable_drain_cannot_authorize_termination(
    setup: tuple[Allocator, MemoryStore, FakeProvider, Drain],
) -> None:
    allocator, _, provider, drain = setup
    allocator.acquire(SCOPE, FIRST)

    def disconnected() -> None:
        raise ProviderUnavailable("control channel unavailable")

    drain.during_drain = disconnected
    with pytest.raises(ProviderUnavailable):
        allocator.acquire(SCOPE, SECOND, replaces=FIRST)
    assert provider.creates == 1 and "pod-1" in provider.pods
    assert not allocator.selected(SCOPE, FIRST, "pod-1")
    assert not any(event[0] == "delete" for event in provider.events)


def test_real_http_adapter_recovers_a_lost_reply_and_replaces_exactly_once() -> None:
    inventory: dict[str, dict[str, object]] = {}
    posts = 0

    def server(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            pod = {**json.loads(request.content), "id": f"pod-{posts}", "status": "RUNNING"}
            inventory[f"pod-{posts}"] = pod
            raise httpx.ReadTimeout("reply lost after creation", request=request)
        if request.url.path == "/v2/pods":
            return httpx.Response(200, json={
                "pods": list(inventory.values()),
                "pagination": {"hasNextPage": False, "nextCursor": None},
            })
        pod_id = request.url.path.rsplit("/", 1)[1]
        if request.method == "DELETE":
            inventory.pop(pod_id, None)
            return httpx.Response(204)
        if pod_id in inventory:
            return httpx.Response(200, json=inventory[pod_id])
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        provider = RunPodClient("offline", http)
        store, drain = MemoryStore(), Drain()
        allocator = Allocator(store, provider, drain, SPEC)
        assert allocator.acquire(SCOPE, FIRST).phase == "creating"
        recovered = Allocator(store, provider, drain, SPEC)
        assert recovered.acquire(SCOPE, FIRST).pod_id == "pod-1"
        assert posts == 1
        assert recovered.acquire(SCOPE, SECOND, replaces=FIRST).phase == "creating"
        assert recovered.acquire(SCOPE, SECOND, replaces=FIRST).pod_id == "pod-2"
        assert list(inventory) == ["pod-2"] and posts == 2
        assert drain.calls == ["pod-1"]

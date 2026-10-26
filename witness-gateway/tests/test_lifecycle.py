from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from support import Provider

from pod_gateway.control import DrainControl
from pod_gateway.lifecycle import AllocationConflict, Allocator, InventoryConflict
from pod_gateway.models import Pod, PodSpec, Scope
from pod_gateway.runpod import CreateUnknown, ProviderUnavailable

SCOPE = Scope("competition", "test-0", "5" * 48)
SPEC = PodSpec("registry/witness@sha256:" + "a" * 64)
FIRST = "00000000-0000-4000-8000-000000000001"
SECOND = "00000000-0000-4000-8000-000000000002"


class Drain:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.calls: list[str] = []

    def drain(self, pod: Pod) -> bool:
        self.calls.append(pod.id)
        return self.ready


def test_restart_discovers_the_current_connection_without_another_create() -> None:
    provider, drain = Provider(), Drain()
    first = Allocator(provider, drain, SPEC).acquire(SCOPE, FIRST)
    restarted = Allocator(provider, drain, SPEC)
    assert restarted.acquire(SCOPE, FIRST) == first
    assert restarted.selected(SCOPE, FIRST, first.pod_id or "")
    assert provider.creates == 1


def test_concurrent_connect_retries_submit_one_create() -> None:
    provider = Provider()
    allocator = Allocator(provider, Drain(), SPEC)
    with ThreadPoolExecutor(max_workers=8) as pool:
        sessions = list(pool.map(lambda _: allocator.acquire(SCOPE, FIRST), range(8)))
    assert provider.creates == 1
    assert len({s.pod_id for s in sessions}) == 1


def test_fresh_allocation_terminates_the_current_pod_without_a_drain_reply() -> None:
    provider, drain = Provider(), Drain(False)
    allocator = Allocator(provider, drain, SPEC)
    old = allocator.acquire(SCOPE, FIRST)
    new = allocator.acquire(SCOPE, SECOND)
    assert new.phase == "allocated" and new.pod_id != old.pod_id
    assert not allocator.selected(SCOPE, FIRST, old.pod_id or "")
    assert old.pod_id not in provider.pods and provider.creates == 2


@pytest.mark.parametrize("reconcile_first", [False, True])
def test_unrecoverable_error_pod_is_replaced_without_a_worker(reconcile_first: bool) -> None:
    provider = Provider()
    allocator = Allocator(provider, DrainControl(), SPEC)
    old = allocator.acquire(SCOPE, FIRST)
    assert old.pod_id is not None
    provider.pods[old.pod_id] = replace(provider.pods[old.pod_id], status="ERROR")
    if reconcile_first:
        retired = allocator.reconcile(SCOPE)
        assert retired is not None and retired.phase == "retired"
    replacement = allocator.acquire(SCOPE, SECOND, replaces=FIRST)
    assert replacement.phase == "allocated" and replacement.pod_id != old.pod_id
    assert old.pod_id not in provider.pods and provider.creates == 2


def test_error_pod_replacement_waits_for_confirmed_deletion() -> None:
    class SlowDelete(Provider):
        hold = True

        def delete(self, pod_id: str) -> None:
            if not self.hold:
                super().delete(pod_id)
            raise ProviderUnavailable("reply lost")

    provider = SlowDelete()
    allocator = Allocator(provider, DrainControl(), SPEC)
    old = allocator.acquire(SCOPE, FIRST)
    assert old.pod_id is not None
    provider.pods[old.pod_id] = replace(provider.pods[old.pod_id], status="ERROR")
    assert allocator.acquire(SCOPE, SECOND, replaces=FIRST).phase == "draining"
    assert provider.creates == 1 and old.pod_id in provider.pods
    provider.hold = False
    replacement = allocator.reconcile(SCOPE)
    assert replacement is not None and replacement.phase == "allocated"
    assert old.pod_id not in provider.pods and provider.creates == 2


def test_lost_create_reply_adopts_the_real_pod_after_restart() -> None:
    class LostReply(Provider):
        def create(self, body: dict[str, object]) -> Pod:
            super().create(body)
            raise CreateUnknown("lost response")

    provider = LostReply()
    assert Allocator(provider, Drain(), SPEC).acquire(SCOPE, FIRST).phase == "creating"
    recovered = Allocator(provider, Drain(), SPEC).reconcile(SCOPE)
    assert recovered is not None and recovered.phase == "allocated"
    assert provider.creates == 1


def test_late_duplicate_stays_dormant_and_cannot_displace_active_capture() -> None:
    provider, drain = Provider(), Drain(False)
    allocator = Allocator(provider, drain, SPEC)
    active = allocator.acquire(SCOPE, FIRST)
    late = provider.create(SPEC.body(SCOPE, FIRST, epoch_ns=1))
    selected = Allocator(provider, drain, SPEC).reconcile(SCOPE)
    assert selected is not None and selected.pod_id == active.pod_id
    assert provider.get(late.id).lifecycle == "draining"  # type: ignore[union-attr]
    assert active.pod_id in provider.pods and late.id in provider.pods
    drain.ready = True
    allocator.reconcile(SCOPE)
    assert late.id not in provider.pods and active.pod_id in provider.pods


def test_late_previous_connection_cannot_replace_its_successor() -> None:
    provider = Provider()
    allocator = Allocator(provider, Drain(), SPEC)
    allocator.acquire(SCOPE, FIRST)
    latest = allocator.acquire(SCOPE, SECOND, replaces=FIRST)
    old = provider.create(SPEC.body(SCOPE, FIRST, epoch_ns=1))
    assert allocator.reconcile(SCOPE) == latest
    assert old.id not in provider.pods


def test_restart_can_repeat_unknown_post_but_duplicate_captures_are_never_activated() -> (
    None
):
    class Delayed(Provider):
        delayed: dict[str, object] | None = None

        def create(self, body: dict[str, object]) -> Pod:
            if self.delayed is None:
                self.delayed = body
                raise CreateUnknown("not visible yet")
            return super().create(body)

    provider = Delayed()
    first = Allocator(provider, Drain(), SPEC)
    assert first.acquire(SCOPE, FIRST).phase == "creating"
    assert first.acquire(SCOPE, FIRST).phase == "creating"
    restarted = Allocator(provider, Drain(), SPEC)
    ready = restarted.acquire(SCOPE, FIRST)
    assert ready.phase == "allocated" and provider.delayed is not None
    late = provider.create(provider.delayed)
    assert restarted.reconcile(SCOPE) == ready
    assert late.id not in provider.pods and len(provider.pods) == 1


def test_unconfirmed_delete_blocks_new_creation_and_counts_old_resource() -> None:
    class SlowDelete(Provider):
        hold = True

        def delete(self, pod_id: str) -> None:
            if not self.hold:
                super().delete(pod_id)
            raise ProviderUnavailable("reply lost")

    provider = SlowDelete()
    allocator = Allocator(provider, Drain(), SPEC)
    old = allocator.acquire(SCOPE, FIRST)
    assert allocator.acquire(SCOPE, SECOND, replaces=FIRST).phase == "draining"
    assert provider.creates == 1 and old.pod_id in provider.pods
    provider.hold = False
    assert allocator.reconcile(SCOPE).phase == "allocated"  # type: ignore[union-attr]
    assert provider.creates == 2


def test_missing_provider_pod_is_missing_after_restart() -> None:
    provider = Provider()
    old = Allocator(provider, Drain(), SPEC).acquire(SCOPE, FIRST)
    provider.pods.pop(old.pod_id or "")
    restarted = Allocator(provider, Drain(), SPEC)
    assert restarted.current(SCOPE) is None
    assert restarted.status(SCOPE, FIRST) is None
    assert restarted.acquire(SCOPE, SECOND).phase == "allocated"


def test_unknown_create_expires_as_an_attempt_without_a_phantom_live_pod() -> None:
    class Unknown(Provider):
        def create(self, body: dict[str, object]) -> Pod:
            raise CreateUnknown("lost")

    now = [1.0]
    allocator = Allocator(Unknown(), Drain(), SPEC, clock=lambda: now[0])
    allocator.acquire(SCOPE, FIRST)
    now[0] = 602
    assert allocator.status(SCOPE, FIRST).phase == "lost"  # type: ignore[union-attr]
    assert allocator.current(SCOPE) is None


def test_foreign_and_malformed_ownership_never_authorize_deletion() -> None:
    provider = Provider()
    unrelated = Pod("foreign", "w404-prefix", SPEC.image, "RUNNING", {})
    provider.pods[unrelated.id] = unrelated
    allocator = Allocator(provider, Drain(), SPEC)
    first = allocator.acquire(SCOPE, FIRST)
    assert first.pod_id is not None
    with pytest.raises(InventoryConflict):
        allocator.retire(SCOPE, FIRST, unrelated.id)
    assert unrelated.id in provider.pods
    with pytest.raises(AllocationConflict):
        Allocator(provider, Drain(), replace(SPEC, vcpu_count=4)).acquire(SCOPE, FIRST)


def test_lost_lifecycle_patch_reply_is_confirmed_from_runpod() -> None:
    class LostPatch(Provider):
        def rename(self, pod_id: str, name: str) -> Pod:
            super().rename(pod_id, name)
            raise ProviderUnavailable("reply lost")

    provider = LostPatch()
    current = Allocator(provider, Drain(), SPEC).acquire(SCOPE, FIRST)
    assert current.phase == "allocated" and provider.creates == 1

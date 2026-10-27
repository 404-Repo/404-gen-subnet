from dataclasses import replace

import pytest
from support import MemoryState, Provider

from pod_gateway.control import DrainControl
from pod_gateway.lifecycle import Allocator, InventoryConflict
from pod_gateway.models import Pod, PodSpec, Scope
from pod_gateway.selection import Selection
from pod_gateway.state import StateConflict

SCOPE = Scope("competition", "test-0", "5" * 48)
START = "00000000-0000-4000-8000-000000000001"
NEXT = "00000000-0000-4000-8000-000000000002"
SPEC = PodSpec("registry/witness@sha256:" + "a" * 64)


def test_durable_selection_fences_replacement_and_survives_restart() -> None:
    state, provider = MemoryState(), Provider()
    selection = Selection(state)
    allocator = Allocator(provider, DrainControl(), SPEC, selection=selection)
    first = allocator.acquire(SCOPE, START)
    assert first.pod_id is not None and selection.selected(SCOPE, START, first.pod_id)
    old = provider.get(first.pod_id)
    assert old is not None
    replacement = allocator.acquire(SCOPE, NEXT, replaces=START)
    assert replacement.phase == "allocated" and first.pod_id not in provider.pods
    assert not Selection(state).selected(SCOPE, START, first.pod_id)
    with pytest.raises(StateConflict):
        selection.update(old, active=True)
    assert replacement.pod_id is not None
    successor = provider.get(replacement.pod_id)
    assert successor is not None and successor.name == old.name
    assert not Selection(state).selected(SCOPE, START, replacement.pod_id)
    assert not Selection(state).selected(SCOPE, NEXT, old.id)
    assert Selection(state).selected(SCOPE, NEXT, replacement.pod_id)
    with pytest.raises(StateConflict):
        selection.update(old, active=True)
    selection.update(old, active=False)
    assert selection.selected(SCOPE, NEXT, replacement.pod_id)


def test_equal_display_prefixes_keep_miners_and_pod_ids_separate_after_restart() -> None:
    state, provider = MemoryState(), Provider()
    other_scope = replace(SCOPE, hotkey=SCOPE.hotkey[:8] + "6" * 40)
    allocator = Allocator(provider, DrainControl(), SPEC, selection=Selection(state))
    first = allocator.acquire(SCOPE, START)
    other = allocator.acquire(other_scope, START)
    assert first.pod_id is not None and other.pod_id is not None
    assert first.pod_id != other.pod_id
    assert provider.pods[first.pod_id].name == provider.pods[other.pod_id].name

    restarted = Allocator(provider, DrainControl(), SPEC, selection=Selection(state))
    assert restarted.acquire(SCOPE, START) == first
    assert restarted.acquire(other_scope, START) == other
    assert provider.creates == 2
    assert Selection(state).selected(SCOPE, START, first.pod_id)
    assert Selection(state).selected(other_scope, START, other.pod_id)
    assert not Selection(state).selected(SCOPE, START, other.pod_id)
    assert not Selection(state).selected(other_scope, START, first.pod_id)
    with pytest.raises(InventoryConflict):
        restarted.retire(SCOPE, START, other.pod_id, drain=False)
    assert restarted.retire(SCOPE, START, first.pod_id, drain=False).phase == "retired"
    assert first.pod_id not in provider.pods and other.pod_id in provider.pods
    assert Selection(state).selected(other_scope, START, other.pod_id)


def test_duplicate_start_cannot_take_an_existing_selection() -> None:
    state, provider = MemoryState(), Provider()
    selection = Selection(state)
    pod = provider.create(SPEC.body(SCOPE, START, epoch_ns=1))
    selection.update(pod, active=True)
    with pytest.raises(StateConflict):
        Selection(state).update(replace(pod, id="late-copy"), active=True)
    assert selection.selected(SCOPE, START, pod.id)


def test_gateway_restart_finishes_a_drain_revoked_before_the_provider_rename() -> None:
    state, provider = MemoryState(), Provider()
    selection = Selection(state)

    class Control:
        def drain(self, pod: Pod) -> bool:
            return True

    allocator = Allocator(provider, Control(), SPEC, selection=selection)
    first = allocator.acquire(SCOPE, START)
    assert first.pod_id is not None
    old = provider.get(first.pod_id)
    assert old is not None
    selection.update(old, active=False)
    restarted = Allocator(provider, Control(), SPEC, selection=Selection(state))
    actual = restarted.reconcile(SCOPE)
    assert actual is not None and actual.phase == "retired"
    assert not selection.selected(SCOPE, START, first.pod_id) and not provider.list()


@pytest.mark.parametrize("revoked", [False, True])
def test_missing_selected_pod_retires_its_newer_duplicate_and_allows_replacement(revoked: bool) -> None:
    state, provider = MemoryState(), Provider()
    selection = Selection(state)

    class Control:
        def drain(self, pod: Pod) -> bool:
            return True

    allocator = Allocator(provider, Control(), SPEC, selection=selection)
    first = allocator.acquire(SCOPE, START)
    assert first.pod_id is not None
    old = provider.get(first.pod_id)
    assert old is not None
    if revoked:
        selection.update(old, active=False)
    provider.delete(old.id)
    late = provider.create(SPEC.body(SCOPE, START, epoch_ns=old.epoch_ns + 1))
    restarted = Allocator(provider, Control(), SPEC, selection=Selection(state))
    retired = restarted.reconcile(SCOPE)
    assert retired is not None and retired.phase == "retired" and provider.get(late.id) is None
    replacement = restarted.acquire(SCOPE, NEXT)
    assert replacement.phase == "allocated" and replacement.pod_id is not None
    assert selection.selected(SCOPE, NEXT, replacement.pod_id)

from dataclasses import replace

import pytest
from support import MemoryState, Provider

from pod_gateway.lifecycle import Allocator
from pod_gateway.models import Pod, PodSpec, Scope
from pod_gateway.selection import Selection
from pod_gateway.state import StateConflict

SCOPE = Scope("competition", "test-0", "5" * 48)
START = "00000000-0000-4000-8000-000000000001"
NEXT = "00000000-0000-4000-8000-000000000002"
SPEC = PodSpec("registry/witness@sha256:" + "a" * 64)


def test_durable_selection_fences_replacement_before_drain_and_survives_restart() -> None:
    state, provider = MemoryState(), Provider()
    selection = Selection(state)
    drained = False

    class Control:
        def drain(self, pod: Pod) -> bool:
            assert not selection.selected(SCOPE, START, pod.id)
            return drained

    allocator = Allocator(provider, Control(), SPEC, selection=selection)
    first = allocator.acquire(SCOPE, START)
    assert first.pod_id is not None and selection.selected(SCOPE, START, first.pod_id)
    old = provider.get(first.pod_id)
    assert old is not None
    waiting = allocator.acquire(SCOPE, NEXT, replaces=START)
    assert waiting.phase == "draining" and provider.creates == 1
    assert not Selection(state).selected(SCOPE, START, first.pod_id)
    with pytest.raises(StateConflict):
        selection.update(old, active=True)
    drained = True
    replacement = allocator.acquire(SCOPE, NEXT, replaces=START)
    assert replacement.pod_id is not None
    assert Selection(state).selected(SCOPE, NEXT, replacement.pod_id)
    with pytest.raises(StateConflict):
        selection.update(old, active=True)
    selection.update(old, active=False)
    assert selection.selected(SCOPE, NEXT, replacement.pod_id)


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

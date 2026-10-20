from __future__ import annotations

from dataclasses import replace

import pytest
from support import MemoryState, Provider

from pod_gateway.admission import Admission, CapacityReached
from pod_gateway.control import DrainControl
from pod_gateway.lifecycle import Allocator
from pod_gateway.models import PodSpec, Scope
from pod_gateway.security import capability, seal


def test_capacity_reservation_survives_restart_and_is_released_only_after_retirement() -> (
    None
):
    state, provider = MemoryState(), Provider()
    secret = b"x" * 32
    scope = Scope("competition", "test-0", "5" * 48)
    spec = PodSpec("registry/witness@sha256:" + "a" * 64)
    admission = Admission(
        state, max_pods=1, hourly_ceiling="0.07", per_pod_ceiling="0.07"
    )
    first = Allocator(
        state, provider, DrainControl(state, secret), spec, admission=admission
    )
    current = first.acquire(scope, "00000000-0000-4000-8000-000000000001")
    other_scope = replace(scope, hotkey="6" * 48)
    restarted = Allocator(
        state, provider, DrainControl(state, secret), spec, admission=admission
    )
    with pytest.raises(CapacityReached):
        restarted.acquire(other_scope, "00000000-0000-4000-8000-000000000002")
    assert provider.creates == 1
    assert current.pod_id is not None
    seal(
        state,
        scope,
        current.start_id,
        current.pod_id,
        capability(secret, scope, current.start_id),
        "drain",
    )
    assert first.retire(scope, current.start_id, current.pod_id).phase == "retired"
    second = restarted.reconcile(other_scope)
    assert second is not None and second.phase == "allocated" and provider.creates == 2

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from support import Provider
from test_lifecycle import FIRST, SCOPE, SECOND, SPEC, Drain

from pod_gateway.admission import Admission, CapacityReached
from pod_gateway.lifecycle import Allocator


def test_capacity_uses_actual_inventory_and_frees_when_the_pod_disappears() -> None:
    provider = Provider()
    policy = Admission(max_pods=1, hourly_ceiling="0.07", per_pod_ceiling="0.07")
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
    policy = Admission(max_pods=2, hourly_ceiling="0.14", per_pod_ceiling="0.07")
    with pytest.raises(CapacityReached):
        policy.check(provider.list(), SCOPE.deployment)
    provider.delete(first.id)
    policy.check(provider.list(), SCOPE.deployment)


def test_inventory_cost_above_quote_and_inflight_attempts_consume_headroom() -> None:
    provider = Provider()
    pod = provider.create(SPEC.body(SCOPE, FIRST))
    provider.pods[pod.id] = replace(pod, hourly_cost="0.11")
    policy = Admission(max_pods=250, hourly_ceiling="0.15", per_pod_ceiling="0.07")
    with pytest.raises(CapacityReached):
        policy.check(provider.list(), SCOPE.deployment)
    with pytest.raises(CapacityReached):
        policy.check([], SCOPE.deployment, pending=2)


def test_concurrent_miners_share_serialized_provider_capacity_admission() -> None:
    provider = Provider()
    allocator = Allocator(
        provider,
        Drain(),
        SPEC,
        admission=Admission(max_pods=1, hourly_ceiling="0.07", per_pod_ceiling="0.07"),
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

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pod import PodRuntime
from support import MemoryState, Provider
from witness_client.calls import canonical

from pod_gateway.budget import Budget
from pod_gateway.control import DrainControl
from pod_gateway.lifecycle import Allocator
from pod_gateway.models import Pod, PodSpec, Scope
from pod_gateway.records import CallRecords
from pod_gateway.revivor import GatewayDrain, Revivor
from pod_gateway.security import capability, sealed

SECRET = b"x" * 32
SCOPE = Scope("competition", "test-0", "5" * 48)
GATEWAY = replace(SCOPE, round="gateway")
SPEC = PodSpec("registry/witness@sha256:" + "a" * 64)


class Published:
    def __init__(self) -> None:
        self.pods: list[str] = []

    def publish(self, pod: Pod) -> None:
        self.pods.append(pod.id)


def test_revivor_replaces_failed_gateway_and_preserves_unrelated_pods() -> None:
    state, provider, published = MemoryState(), Provider(), Published()
    gateway = Allocator(state, provider, GatewayDrain(GATEWAY), SPEC)
    workers = Allocator(state, provider, DrainControl(state, SECRET), SPEC)
    old = gateway.acquire(GATEWAY, "00000000-0000-4000-8000-000000000001")
    provider.pods["unrelated"] = Pod(
        "unrelated", "someone-else", SPEC.image, "RUNNING", {}
    )
    healthy = [True]
    revivor = Revivor(
        state=state,
        provider=provider,
        gateway=gateway,
        workers=workers,
        scope=GATEWAY,
        active_round="test-0",
        secret=SECRET,
        publisher=published,
        probe=lambda _: healthy[0],
        startup_grace=600,
    )
    revivor.tick()
    healthy[0] = False
    for _ in range(3):
        revivor.tick()
    current = gateway.current(GATEWAY)
    assert (
        current is not None
        and current.phase == "allocated"
        and current.start_id != old.start_id
    )
    assert old.pod_id not in provider.pods
    assert "unrelated" in provider.pods


def test_new_gateway_gets_boot_grace_and_config_changes_replace_it() -> None:
    state, provider = MemoryState(), Provider()
    gateway = Allocator(state, provider, GatewayDrain(GATEWAY), SPEC)
    workers = Allocator(state, provider, DrainControl(state, SECRET), SPEC)
    old = gateway.acquire(GATEWAY, "00000000-0000-4000-8000-000000000001")
    now = [0.0]
    revivor = Revivor(
        state=state,
        provider=provider,
        gateway=gateway,
        workers=workers,
        scope=GATEWAY,
        active_round="test-0",
        secret=SECRET,
        publisher=Published(),
        probe=lambda _: False,
        clock=lambda: now[0],
        startup_grace=600,
    )
    for _ in range(3):
        revivor.tick()
    assert gateway.current(GATEWAY) == old
    now[0] = 601
    revivor.tick()
    current = gateway.current(GATEWAY)
    assert current is not None and current.start_id != old.start_id
    gateway.spec = replace(SPEC, vcpu_count=4)
    revivor.tick()
    changed = gateway.current(GATEWAY)
    assert changed is not None and changed.start_id != current.start_id


def test_idle_expiry_waits_for_capture_and_upload_completion(tmp_path: Path) -> None:
    state, provider = MemoryState(), Provider()
    allocator = Allocator(state, provider, DrainControl(state, SECRET), SPEC)
    session = allocator.acquire(SCOPE, "00000000-0000-4000-8000-000000000001")
    assert session.pod_id is not None
    now = [0.0]
    pricing = canonical(
        {"calls_per_prompt": 60, "credits_per_prompt": 5_000_000, "models": {}}
    )
    runtime = PodRuntime(
        state=state,
        evidence=state,
        records=CallRecords(state),
        budget=Budget(state, pricing),
        scope=SCOPE,
        start_id=session.start_id,
        pod_id=session.pod_id,
        token=capability(SECRET, SCOPE, session.start_id),
        origin="https://witness.test",
        proof_hash="a" * 64,
        round_end=9999999999,
        spool=tmp_path / "spool",
        clock=lambda: now[0],
    )
    runtime.active["capture-with-pending-upload"] = None
    now[0] = 1801
    runtime.drain()
    assert not runtime.tick()
    assert not sealed(state, SCOPE, session.start_id, session.pod_id, runtime.token)
    runtime.active.clear()
    assert runtime.tick()
    retired = allocator.retire(SCOPE, session.start_id, session.pod_id)
    assert retired.phase == "retired" and session.pod_id not in provider.pods
    runtime.executor.shutdown(wait=True)

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pod import PodRuntime
from support import MemoryState, Provider
from test_lifecycle import Drain
from witness_client.calls import canonical

from pod_gateway.admission import Admission
from pod_gateway.budget import Budget
from pod_gateway.lifecycle import Allocator
from pod_gateway.models import Pod, PodSpec, Scope
from pod_gateway.records import CallRecords
from pod_gateway.revivor import CaddyPublisher, GatewayDrain, Revivor
from pod_gateway.security import capability

SECRET = b"x" * 32
SCOPE = Scope("competition", "test-0", "5" * 48)
GATEWAY = replace(SCOPE, round="gateway")
SPEC = PodSpec("registry/witness@sha256:" + "a" * 64)


def test_published_config_stays_readable_by_caddy_group(tmp_path: Path) -> None:
    provider = Provider()
    pod = provider.create(SPEC.body(GATEWAY, "00000000-0000-4000-8000-000000000001"))
    config = tmp_path / "Caddyfile"
    publisher = CaddyPublisher(
        origin="https://witness.test", ca_file=tmp_path / "ca.pem", config=config, executable="/usr/bin/true"
    )
    publisher.publish(pod)
    assert config.stat().st_mode & 0o777 == 0o640
    assert "https://8.8.8.8:44001" in config.read_text()
    publisher.publish(replace(pod, tcp={8443: ("8.8.4.4", 44002)}))
    assert config.stat().st_mode & 0o777 == 0o640
    assert "https://8.8.4.4:44002" in config.read_text()


def test_publisher_can_bind_a_loopback_frontend_for_a_test_tunnel(tmp_path: Path) -> None:
    provider = Provider()
    pod = provider.create(SPEC.body(GATEWAY, "00000000-0000-4000-8000-000000000001"))
    config = tmp_path / "Caddyfile"
    publisher = CaddyPublisher(
        origin="https://witness.test", ca_file=tmp_path / "ca.pem", config=config,
        executable="/usr/bin/true", listen="http://127.0.0.1:18051",
    )
    publisher.publish(pod)
    assert config.read_text().startswith('"http://127.0.0.1:18051" {')
    assert "https://8.8.8.8:44001" in config.read_text()
    assert "tls_server_name gateway.internal" in config.read_text()


class Published:
    def __init__(self) -> None:
        self.pods: list[str] = []

    def publish(self, pod: Pod) -> None:
        self.pods.append(pod.id)


def test_revivor_replaces_failed_gateway_and_preserves_unrelated_pods() -> None:
    provider, published = Provider(), Published()
    gateway = Allocator(provider, GatewayDrain(GATEWAY), SPEC)
    workers = Allocator(provider, Drain(), SPEC)
    old = gateway.acquire(GATEWAY, "00000000-0000-4000-8000-000000000001")
    provider.pods["unrelated"] = Pod(
        "unrelated", "someone-else", SPEC.image, "RUNNING", {}
    )
    healthy = [True]
    revivor = Revivor(
        provider=provider,
        gateway=gateway,
        workers=workers,
        scope=GATEWAY,
        active_round="test-0",
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
    provider = Provider()
    gateway = Allocator(provider, GatewayDrain(GATEWAY), SPEC)
    workers = Allocator(provider, Drain(), SPEC)
    old = gateway.acquire(GATEWAY, "00000000-0000-4000-8000-000000000001")
    now = [0.0]
    revivor = Revivor(
        provider=provider,
        gateway=gateway,
        workers=workers,
        scope=GATEWAY,
        active_round="test-0",
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
    allocator = Allocator(provider, Drain(), SPEC)
    session = allocator.acquire(SCOPE, "00000000-0000-4000-8000-000000000001")
    assert session.pod_id is not None
    now = [0.0]
    pricing = canonical(
        {"calls_per_prompt": 60, "credits_per_prompt": 5_000_000, "models": {}}
    )
    runtime = PodRuntime(
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
    runtime.active.clear()
    assert runtime.tick()
    provider.pods[session.pod_id] = replace(
        provider.pods[session.pod_id], status="EXITED"
    )
    retired = allocator.retire(SCOPE, session.start_id, session.pod_id)
    assert retired.phase == "retired" and session.pod_id not in provider.pods
    runtime.executor.shutdown(wait=True)


def test_cleanup_adopts_a_late_worker_instead_of_deleting_it() -> None:
    provider = Provider()
    pending = provider.create(
        SPEC.body(SCOPE, "00000000-0000-4000-8000-000000000001", epoch_ns=1)
    )
    workers = Allocator(provider, Drain(), SPEC)
    supervisor = Revivor(
        provider=provider,
        gateway=Allocator(provider, GatewayDrain(GATEWAY), SPEC),
        workers=workers,
        scope=GATEWAY,
        active_round="test-0",
        publisher=Published(),
        probe=lambda _: True,
    )
    supervisor.cleanup_workers()
    selected = workers.current(SCOPE)
    assert (
        selected is not None
        and selected.pod_id == pending.id
        and selected.phase == "allocated"
    )
    assert provider.creates == 1


def test_cleanup_removes_only_drained_duplicates_and_old_round_workers() -> None:
    provider = Provider()
    workers = Allocator(provider, Drain(), SPEC)
    active = workers.acquire(SCOPE, "00000000-0000-4000-8000-000000000001")
    duplicate = provider.create(SPEC.body(SCOPE, active.start_id, epoch_ns=1))
    old_scope = replace(SCOPE, round="test-old")
    old = provider.create(
        SPEC.body(old_scope, "00000000-0000-4000-8000-000000000002", epoch_ns=1)
    )
    unrelated = Pod("unrelated", "w404-prefix", SPEC.image, "RUNNING", {})
    provider.pods[unrelated.id] = unrelated
    supervisor = Revivor(
        provider=provider,
        gateway=Allocator(provider, GatewayDrain(GATEWAY), SPEC),
        workers=workers,
        scope=GATEWAY,
        active_round="test-0",
        publisher=Published(),
        probe=lambda _: True,
    )
    supervisor.cleanup_workers()
    assert active.pod_id in provider.pods and unrelated.id in provider.pods
    assert duplicate.id not in provider.pods and old.id not in provider.pods


def test_cleanup_drains_a_unique_late_create_that_exceeds_actual_capacity() -> None:
    provider, drain = Provider(), Drain(False)
    workers = Allocator(
        provider,
        drain,
        SPEC,
        admission=Admission(max_pods=1, hourly_ceiling="0.07", per_pod_ceiling="0.07"),
    )
    active = workers.acquire(SCOPE, "00000000-0000-4000-8000-000000000001")
    other = replace(SCOPE, hotkey="6" * 48)
    late = provider.create(
        SPEC.body(other, "00000000-0000-4000-8000-000000000002", epoch_ns=1)
    )
    supervisor = Revivor(
        provider=provider,
        gateway=Allocator(provider, GatewayDrain(GATEWAY), SPEC),
        workers=workers,
        scope=GATEWAY,
        active_round="test-0",
        publisher=Published(),
        probe=lambda _: True,
    )
    supervisor.cleanup_workers()
    assert provider.get(late.id).lifecycle == "draining"  # type: ignore[union-attr]
    assert active.pod_id in provider.pods and late.id in provider.pods
    drain.ready = True
    supervisor.cleanup_workers()
    assert late.id not in provider.pods and active.pod_id in provider.pods

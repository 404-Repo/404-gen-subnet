"""Independent gateway recovery and cleanup of exactly owned, drained Pods."""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import ssl
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .admission import Admission
from .control import DrainControl
from .lifecycle import Allocator, InventoryConflict
from .models import Pod, Scope, Session
from .runpod import PodProvider, RunPodClient
from .settings import Settings


class Publisher(Protocol):
    def publish(self, pod: Pod) -> None: ...


def endpoint(pod: Pod) -> str:
    address, port = pod.tcp[8443]
    parsed = ipaddress.ip_address(address)
    if not parsed.is_global:
        raise ValueError("Gateway TCP endpoint is not public")
    return f"[{parsed}]:{port}" if parsed.version == 6 else f"{parsed}:{port}"


class CaddyPublisher:
    def __init__(
        self, *, origin: str, ca_file: Path, config: Path, executable: str = "caddy"
    ) -> None:
        self.hostname = urlsplit(origin).hostname
        self.ca_file, self.config, self.executable = ca_file, config, executable
        self.published: bytes | None = None

    def publish(self, pod: Pod) -> None:
        data = (
            f"{json.dumps(self.hostname)} {{\n"
            f"  reverse_proxy https://{endpoint(pod)} {{\n"
            "    transport http {\n"
            "      tls_server_name gateway.internal\n"
            f"      tls_trust_pool file {json.dumps(str(self.ca_file))}\n"
            "    }\n  }\n}\n"
        ).encode()
        if data != self.published or not self.config.is_file():
            fd, temporary = tempfile.mkstemp(
                prefix=".witness-upstream-", dir=self.config.parent
            )
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.config)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        # Caddy ignores identical reloads. Reassert the config after a frontend
        # restart as well; the in-memory publisher cache is not its live state.
        subprocess.run(
            [
                self.executable,
                "reload",
                "--config",
                str(self.config),
                "--adapter",
                "caddyfile",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        self.published = data


class GatewayDrain:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    def drain(self, pod: Pod) -> bool:
        # Gateways do not capture providers. Revoking this allocation fences its
        # HTTP admission; worker capture continues independently during recovery.
        return pod.in_scope(self.scope)


class Revivor:
    def __init__(
        self,
        *,
        provider: PodProvider,
        gateway: Allocator,
        workers: Allocator,
        scope: Scope,
        active_round: str,
        publisher: Publisher,
        probe: Callable[[Pod], bool],
        startup_grace: int = 600,
        failures: int = 3,
        clock: Callable[[], float] = time.time,
        round_end: int | None = None,
    ) -> None:
        self.provider, self.gateway, self.workers = provider, gateway, workers
        self.health: dict[str, dict[str, object]] = {}
        self.scope, self.active_round = scope, active_round
        self.publisher, self.probe = publisher, probe
        self.startup_grace, self.failures, self.clock = startup_grace, failures, clock
        self.round_end = round_end

    def _health(self, current: Session, healthy: bool) -> bool:
        value = self.health.setdefault(
            current.start_id,
            {"first_seen": self.clock(), "ready_once": False, "failures": 0},
        )
        value["ready_once"] = value["ready_once"] is True or healthy
        value["failures"] = 0 if healthy else int(str(value["failures"])) + 1
        return (
            not healthy
            and int(str(value["failures"])) >= self.failures
            and (
                value["ready_once"] is True
                or self.clock() - float(str(value["first_seen"])) >= self.startup_grace
            )
        )

    def recover_gateway(self) -> None:
        current = self.gateway.current(self.scope)
        if current is None:
            self.gateway.acquire(self.scope, str(uuid.uuid4()))
            return
        if current.phase in ("creating", "draining", "retiring"):
            self.gateway.reconcile(self.scope)
            return
        if current.phase in ("lost", "rejected", "retired"):
            self.gateway.acquire(
                self.scope, str(uuid.uuid4()), replaces=current.start_id
            )
            return
        if current.spec_hash != self.gateway.spec.fingerprint:
            self.gateway.acquire(
                self.scope, str(uuid.uuid4()), replaces=current.start_id
            )
            return
        assert current.pod_id is not None
        pod = self.provider.get(current.pod_id)
        if pod is None:
            self.gateway.reconcile(self.scope)
            return
        if not pod.matches(self.scope, current):
            raise InventoryConflict("Gateway identity changed")
        try:
            healthy = pod.status == "RUNNING" and 8443 in pod.tcp and self.probe(pod)
        except (httpx.HTTPError, ValueError, KeyError):
            healthy = False
        if healthy:
            # Reload only an exactly selected gateway. A frontend outage alone
            # does not cause the direct, CA-verified probe to replace its Pod.
            latest = self.gateway.current(self.scope)
            if (
                latest is not None
                and latest.start_id == current.start_id
                and latest.phase == "allocated"
            ):
                self.publisher.publish(pod)
        if self._health(current, healthy):
            self.gateway.acquire(
                self.scope, str(uuid.uuid4()), replaces=current.start_id
            )

    def cleanup_workers(self) -> None:
        inventory = self.provider.list(fresh=True)
        if self.workers.admission is not None:
            for pod in self.workers.admission.excess(inventory, self.scope.deployment):
                scope = pod.scope()
                assert scope is not None
                self.workers.retire(scope, pod.markers["WITNESS_START_ID"], pod.id)
            inventory = self.provider.list(fresh=True)
        scopes = {p.scope() for p in inventory}
        for scope in scopes:
            if (
                scope is None
                or scope.deployment != self.scope.deployment
                or scope.round == "gateway"
            ):
                continue
            try:
                pods = self.workers.owned(inventory, scope)
                if scope.round != self.active_round or (
                    self.round_end is not None and self.clock() >= self.round_end
                ):
                    for pod in pods:
                        self.workers.retire(
                            scope, pod.markers["WITNESS_START_ID"], pod.id
                        )
                else:
                    # Adopt the sole valid late create, retain an active winner,
                    # and retire only actual superseded/duplicate RunPod Pods.
                    self.workers.reconcile(scope)
            except (ValueError, KeyError, InventoryConflict):
                continue

    def tick(self) -> None:
        try:
            self.recover_gateway()
        finally:
            self.cleanup_workers()


def run(settings: Settings) -> None:
    lock = Path(
        settings.env.get(
            "WITNESS_REVIVOR_LOCK",
            str(Path(settings.env["CADDY_CONFIG_FILE"]).parent / "revivor.lock"),
        )
    )
    with lock.open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _run(settings)


def _run(settings: Settings) -> None:
    with (
        RunPodClient.http_client() as http,
        httpx.Client(
            verify=ssl.create_default_context(
                cafile=settings.env["GATEWAY_TLS_CA_FILE"]
            ),
            timeout=10,
            follow_redirects=False,
        ) as health,
    ):
        provider = RunPodClient(
            settings.env["GATEWAY_RUNPOD_API_KEY"],
            http,
            min_interval=float(settings.env.get("RUNPOD_MIN_REQUEST_INTERVAL", "1.1")),
            cache_seconds=2,
        )
        gateway = Allocator(
            provider,
            GatewayDrain(settings.gateway_scope),
            settings.gateway_spec(),
        )
        admission = Admission(
            max_pods=int(settings.env.get("WITNESS_MAX_PODS", "250")),
            hourly_ceiling=str(settings.worker_budget),
            per_pod_ceiling=str(settings.pod_ceiling),
        )
        workers = Allocator(
            provider,
            DrainControl(),
            settings.worker_spec(),
            admission=admission,
        )

        def probe(pod: Pod) -> bool:
            request = health.build_request("GET", f"https://{endpoint(pod)}/health")
            request.extensions["sni_hostname"] = "gateway.internal"
            response = health.send(request)
            return (
                response.status_code == 200
                and response.json().get("transport") == "pods-v1"
            )

        revivor = Revivor(
            provider=provider,
            gateway=gateway,
            workers=workers,
            scope=settings.gateway_scope,
            active_round=settings.round,
            round_end=settings.round_end,
            publisher=CaddyPublisher(
                origin=settings.env["WITNESS_GATEWAY_URL"],
                ca_file=Path(settings.env["GATEWAY_TLS_CA_FILE"]),
                config=Path(settings.env["CADDY_CONFIG_FILE"]),
            ),
            probe=probe,
        )
        while True:
            try:
                revivor.tick()
            except Exception as error:  # noqa: BLE001 - persistent supervisor, no secret diagnostics
                print(
                    json.dumps(
                        {"event": "revivor_retry", "error_type": type(error).__name__}
                    ),
                    flush=True,
                )
            time.sleep(30)

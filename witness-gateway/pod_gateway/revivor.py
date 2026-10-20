"""Independent gateway recovery and cleanup of exactly owned, drained Pods."""

from __future__ import annotations

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
from .models import Journal, Pod, Scope, Session, json_bytes, object_dict
from .runpod import PodProvider, ProviderUnavailable, RunPodClient
from .security import capability, seal, sealed
from .settings import Settings
from .state import StateConflict, StateStore


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
        state: StateStore,
        provider: PodProvider,
        gateway: Allocator,
        workers: Allocator,
        scope: Scope,
        active_round: str,
        secret: bytes,
        publisher: Publisher,
        probe: Callable[[Pod], bool],
        startup_grace: int = 600,
        failures: int = 3,
        clock: Callable[[], float] = time.time,
        round_end: int | None = None,
    ) -> None:
        self.state, self.provider, self.gateway, self.workers = (
            state,
            provider,
            gateway,
            workers,
        )
        self.scope, self.active_round, self.secret = scope, active_round, secret
        self.publisher, self.probe = publisher, probe
        self.startup_grace, self.failures, self.clock = startup_grace, failures, clock
        self.round_end = round_end

    def _health(self, current: Session, healthy: bool) -> bool:
        key = f"pod-revivor/v1/{self.scope.deployment}/{current.start_id}.json"
        saved = self.state.load(key)
        value: dict[str, object] = (
            object_dict(json.loads(saved.data))
            if saved
            else {"first_seen": self.clock(), "ready_once": False, "failures": 0}
        )
        value["ready_once"] = value["ready_once"] is True or healthy
        value["failures"] = 0 if healthy else int(str(value["failures"])) + 1
        value["revision"] = str(uuid.uuid4())
        self.state.save(key, json_bytes(value), saved.etag if saved else None)
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
            try:
                self.gateway.reconcile(self.scope)
            except InventoryConflict:
                # Extra gateway candidates are dormant: none can pass the R2
                # selection fence. Keep one, terminate other exact candidates.
                candidates = [
                    p for p in self.provider.list() if p.matches(self.scope, current)
                ]
                if len(candidates) < 2:
                    raise
                for candidate in sorted(candidates, key=lambda p: p.id)[1:]:
                    self._delete(candidate)
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

    def _delete(self, pod: Pod) -> bool:
        key = f"pod-cleanup/v1/{self.scope.deployment}/{pod.id}.json"
        saved = self.state.load(key)
        if saved is None:
            try:
                self.state.save(
                    key,
                    json_bytes(
                        {
                            "pod_id": pod.id,
                            "name": pod.name,
                            "markers": dict(pod.markers),
                            "revision": str(uuid.uuid4()),
                        }
                    ),
                    None,
                )
            except StateConflict:
                return False
        else:
            value = object_dict(json.loads(saved.data))
            if (
                value.get("pod_id") != pod.id
                or value.get("name") != pod.name
                or value.get("markers") != dict(pod.markers)
            ):
                raise InventoryConflict("Cleanup identity changed")
        actual = self.provider.get(pod.id)
        if actual is not None:
            if (
                actual.name != pod.name
                or actual.image != pod.image
                or dict(actual.markers) != dict(pod.markers)
            ):
                raise InventoryConflict("Cleanup Pod no longer matches")
            try:
                self.provider.delete(pod.id)
            except ProviderUnavailable:
                pass
        return self.provider.get(pod.id) is None

    def cleanup_workers(self) -> None:
        inventory = self.provider.list()
        for pod in inventory:
            if (
                pod.markers.get("WITNESS_DEPLOYMENT") != self.scope.deployment
                or pod.markers.get("WITNESS_ROUND") == "gateway"
            ):
                continue
            try:
                scope = Scope(
                    self.scope.deployment,
                    pod.markers["WITNESS_ROUND"],
                    pod.markers["WITNESS_HOTKEY"],
                )
                start_id = pod.markers["WITNESS_START_ID"]
                saved = self.state.load(scope.key)
                journal = None if saved is None else Journal.decode(saved.data, scope)
                session = (
                    None
                    if journal is None
                    else next(
                        (s for s in journal.sessions if s.start_id == start_id), None
                    )
                )
                if session is None:
                    session = Session(
                        start_id,
                        None,
                        "creating",
                        self.workers.spec.fingerprint,
                        self.workers.spec.image,
                    )
                if not pod.matches(scope, session):
                    continue
                selected = (
                    journal is not None
                    and journal.current == start_id
                    and session.pod_id == pod.id
                    and session.phase == "allocated"
                )
                token = capability(self.secret, scope, start_id)
                if pod.status != "EXITED" and not sealed(
                    self.state, scope, start_id, pod.id, token
                ):
                    if (
                        not selected
                        or scope.round != self.active_round
                        or (
                            self.round_end is not None
                            and self.clock() >= self.round_end
                        )
                    ):
                        seal(
                            self.state, scope, start_id, pod.id, token, "request-drain"
                        )
                    continue
                if selected:
                    self.workers.retire(scope, start_id, pod.id)
                elif self._delete(pod) and self.workers.admission is not None:
                    # A duplicate of the current start shares its reservation.
                    current_start = journal is not None and journal.current == start_id
                    siblings = any(
                        p.id != pod.id
                        and p.in_scope(scope)
                        and p.markers.get("WITNESS_START_ID") == start_id
                        for p in inventory
                    )
                    if not current_start and not siblings:
                        self.workers.admission.release(scope, start_id)
            except (ValueError, KeyError, StateConflict, InventoryConflict):
                # Preserve resources with ambiguous ownership; retry next tick.
                continue

    def tick(self) -> None:
        try:
            self.recover_gateway()
        finally:
            self.cleanup_workers()


def run(settings: Settings) -> None:
    state = settings.state()
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
        )
        gateway = Allocator(
            state,
            provider,
            GatewayDrain(settings.gateway_scope),
            settings.gateway_spec(),
        )
        admission = Admission(
            state,
            max_pods=int(settings.env.get("WITNESS_MAX_PODS", "250")),
            hourly_ceiling=str(settings.worker_budget),
            per_pod_ceiling=str(settings.pod_ceiling),
        )
        workers = Allocator(
            state,
            provider,
            DrainControl(state, settings.secret),
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
            state=state,
            provider=provider,
            gateway=gateway,
            workers=workers,
            scope=settings.gateway_scope,
            active_round=settings.round,
            secret=settings.secret,
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

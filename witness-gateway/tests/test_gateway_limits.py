from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from typing import cast

from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from bittensor_wallet import Keypair
import pytest
from support import MemoryState, Provider
from witness_client.auth import hotkey_headers

from pod_gateway.admission import Admission, PendingStart, QueueState
from pod_gateway.auth import Authenticator, Membership
from pod_gateway.control import DrainControl, Peer, Registry
from pod_gateway.lifecycle import Allocator
from pod_gateway.models import Pod, PodSpec, Scope, json_bytes
from pod_gateway.runpod import CreateRejected, CreateUnknown, ProviderUnavailable
from pod_gateway.selection import Selection
from pod_gateway.security import capability
from pod_gateway.server import Gateway
from pod_gateway.state import Snapshot, WriteUncertain

KEYS = (Keypair.create_from_uri("//Alice"), Keypair.create_from_uri("//Bob"))
FIRST = "00000000-0000-4000-8000-000000000001"
SECOND = "00000000-0000-4000-8000-000000000002"
pytestmark = pytest.mark.filterwarnings("ignore:It is recommended to use web.RequestKey instances")


class PausedProvider(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.pause = ""
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()

    def wait(self, operation: str) -> None:
        if self.pause == operation:
            self.entered.set()
            assert self.release.wait(5)

    def list(self, *, fresh: bool = False) -> list[Pod]:
        self.wait("list")
        return super().list(fresh=fresh)

    def create(self, body: dict[str, object]) -> Pod:
        self.wait("create")
        pod = replace(super().create(body), tcp={8000: ("8.8.8.8", 44001)})
        self.pods[pod.id] = pod
        return pod


@asynccontextmanager
async def running(provider: Provider, state: MemoryState, *, interval: float = 0,
                  create_interval: float = 10,
                  poll_seconds: float = 0.02,
                  clock: list[float] | None = None) -> AsyncIterator[tuple[Gateway, TestClient[web.Request, web.Application]]]:
    membership = Membership("test", 17, "test-1")
    now = int(time.time() * 1000)
    membership.snapshot = dict(version=1, network="finney", netuid=17, block=1,
                               fetched_at=now, block_time=now, hotkeys=[key.ss58_address for key in KEYS])
    registry = Registry()
    allocator = Allocator(provider, DrainControl(registry), PodSpec("registry/witness@sha256:" + "a" * 64),
                          admission=Admission(max_pods=4), selection=Selection(state),
                          clock=time.time if clock is None else lambda: clock[0])
    gateway = Gateway(allocator=allocator, auth=Authenticator(membership), registry=registry,
                      deployment="test", round_number="test-1", round_end=int(time.time()) + 86400,
                      secret=b"s" * 32, proof_hash="c" * 64, pricing_hash="d" * 64, fence=lambda: True,
                      request_interval=interval, create_interval=create_interval,
                      poll_seconds=poll_seconds, refresh_seconds=0.01, body_timeout=0.05)
    async with TestClient(TestServer(gateway.application())) as client:
        async with asyncio.timeout(2):
            while True:
                response = await client.get("/health")
                if (await response.json())["admission_ready"]:
                    break
                await asyncio.sleep(0.01)
        try:
            yield gateway, client
        finally:
            if isinstance(provider, PausedProvider):
                provider.release.set()


async def request(client: TestClient[web.Request, web.Application], key: Keypair, method: str, path: str,
                  value: dict[str, object] | None = None, *, wait: int | None = None) -> ClientResponse:
    body = b"" if value is None else json_bytes(value)
    headers = hotkey_headers(key, body, method=method, path=path)
    if wait is not None:
        headers["Prefer"] = f"wait={wait}"
    return await client.request(method, path, data=body, headers=headers)


@asynccontextmanager
async def coordinator(provider: Provider, state: MemoryState, clock: list[float], *,
                      keys: tuple[Keypair, ...] = KEYS) -> AsyncIterator[Gateway]:
    membership = Membership("test", 17, "test-1")
    now = int(time.time() * 1000)
    membership.snapshot = dict(version=1, network="finney", netuid=17, block=1,
                               fetched_at=now, block_time=now, hotkeys=[key.ss58_address for key in keys])
    registry = Registry()
    allocator = Allocator(provider, DrainControl(registry),
                          PodSpec("registry/witness@sha256:" + "a" * 64, port=8443),
                          admission=Admission(max_pods=250), selection=Selection(state), clock=lambda: clock[0])
    gateway = Gateway(allocator=allocator, auth=Authenticator(membership), registry=registry,
                      deployment="test", round_number="test-1", round_end=int(time.time()) + 86400,
                      secret=b"s" * 32, proof_hash="c" * 64, pricing_hash="d" * 64, fence=lambda: True)
    gateway._loaded = gateway._fence_ok = True
    gateway._fence_at = time.monotonic()
    try:
        yield gateway
    finally:
        for executor in (gateway._worker, gateway._journal, gateway._verifiers, gateway._fencing):
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)


def test_hotkey_rate_limit_covers_fresh_ids_without_blocking_other_miners() -> None:
    async def scenario() -> None:
        provider = Provider()
        async with running(provider, MemoryState(), interval=60) as (_, client):
            for _ in range(2):
                response = await request(client, KEYS[0], "GET", "/v1/pods/current")
                assert response.status == 200
                await response.read()
            for _ in range(20):
                response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                         {"start_id": str(uuid.uuid4())})
                assert response.status == 429 and response.headers["Retry-After"] == "60"
                await response.read()
            assert (await request(client, KEYS[1], "GET", "/v1/pods/current")).status == 200
            assert (await client.get("/health")).status == 200
            assert provider.creates == 0

    asyncio.run(scenario())


def test_invalid_signatures_cannot_spend_another_hotkeys_allowance() -> None:
    async def scenario() -> None:
        async with running(Provider(), MemoryState(), interval=60) as (_, client):
            for _ in range(3):
                headers = hotkey_headers(KEYS[0], b"", method="GET", path="/v1/pods/current")
                headers["x-bittensor-signature"] = "0x" + "00" * 64
                response = await client.get("/v1/pods/current", headers=headers)
                assert response.status == 401
                await response.read()
            for _ in range(2):
                assert (await request(client, KEYS[0], "GET", "/v1/pods/current")).status == 200
            assert (await request(client, KEYS[0], "GET", "/v1/pods/current")).status == 429

    asyncio.run(scenario())


def test_cached_status_and_unauthenticated_rejection_survive_a_stalled_provider() -> None:
    async def scenario() -> None:
        provider = PausedProvider()
        async with running(provider, MemoryState()) as (gateway, client):
            response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                     {"start_id": FIRST})
            assert response.status == 202
            await response.read()
            async with asyncio.timeout(2):
                while not provider.pods:
                    await asyncio.sleep(0.01)
            pod = next(iter(provider.pods.values()))
            scope = Scope("test", "test-1", KEYS[0].ss58_address)
            gateway.registry.peers[pod.id] = Peer(cast(web.WebSocketResponse, type("Socket", (), {"closed": False})()),
                                                 scope, FIRST, pod.id, ready=True, certificate="offline-ca")
            async with asyncio.timeout(2):
                while True:
                    response = await request(client, KEYS[0], "GET", f"/v1/pods/{FIRST}")
                    saved = await response.json()
                    if saved["witness"] is not None:
                        break
                    await asyncio.sleep(0.02)
            provider.pause = "list"
            provider.release.clear()
            async with asyncio.timeout(1):
                while not provider.entered.is_set():
                    await asyncio.sleep(0.01)
                for _ in range(10):
                    response = await request(client, KEYS[0], "GET", f"/v1/pods/{FIRST}")
                    assert response.status == 200 and (await response.json())["witness"] == saved["witness"]
                response = await client.get(f"/v1/control/{KEYS[0].ss58_address}/{FIRST}",
                                            headers={"Authorization": "Bearer invalid", "x-witness-pod": pod.id})
                assert response.status == 401
                assert (await client.get("/health")).status == 200
                assert (await request(client, KEYS[1], "GET", "/v1/pods/current")).status == 200
                response = await request(client, KEYS[1], "POST", "/v1/pods/start",
                                         {"start_id": SECOND})
                assert response.status == 202 and (await response.json())["session"]["start_id"] == SECOND
            assert provider.creates == 1

    asyncio.run(scenario())


def test_permanent_drain_survives_control_disconnect() -> None:
    async def scenario() -> None:
        provider = Provider()
        async with running(provider, MemoryState()) as (gateway, client):
            scope = gateway.scope(KEYS[0].ss58_address)
            response = await request(client, KEYS[0], "POST", "/v1/pods/start", {"start_id": FIRST})
            assert response.status == 202
            await response.read()
            async with asyncio.timeout(2):
                while not gateway._pods:
                    await asyncio.sleep(0.01)
            pod = next(iter(gateway._pods.values()))
            gateway._due[scope] = time.time() + 60
            async with client.ws_connect(f"/v1/control/{scope.hotkey}/{FIRST}", headers={
                "Authorization": "Bearer " + capability(gateway.secret, scope, FIRST),
                "x-witness-pod": pod.id,
            }) as socket:
                assert (await socket.receive_json())["type"] == "activate"
                await socket.send_json({"type": "drained"})
            async with asyncio.timeout(2):
                while pod.id in gateway.registry.peers:
                    await asyncio.sleep(0.01)
            gateway._due.pop(scope, None)
            async with asyncio.timeout(2):
                while provider.get(pod.id) is not None:
                    await asyncio.sleep(0.01)
            assert provider.creates == 1

    asyncio.run(scenario())


def test_spontaneous_drain_keeps_the_outstanding_control_reply_readable() -> None:
    async def scenario() -> None:
        provider = Provider()
        async with running(provider, MemoryState()) as (gateway, client):
            scope = gateway.scope(KEYS[0].ss58_address)
            response = await request(client, KEYS[0], "POST", "/v1/pods/start", {"start_id": FIRST})
            assert response.status == 202
            await response.read()
            async with asyncio.timeout(2):
                while not gateway._pods:
                    await asyncio.sleep(0.01)
            pod = next(iter(gateway._pods.values()))
            gateway._due[scope] = time.time() + 60
            async with client.ws_connect(f"/v1/control/{scope.hotkey}/{FIRST}", headers={
                "Authorization": "Bearer " + capability(gateway.secret, scope, FIRST), "x-witness-pod": pod.id,
            }) as socket:
                assert (await socket.receive_json())["type"] == "activate"
                retiring = asyncio.create_task(gateway._run(
                    gateway._worker, partial(gateway.allocator.retire, scope, FIRST, pod.id),
                ))
                try:
                    peer = gateway.registry.peers[pod.id]
                    async with asyncio.timeout(2):
                        while not peer.replies:
                            await asyncio.sleep(0.01)
                    await socket.send_json({"type": "drained"})
                    command = await socket.receive_json(timeout=2)
                    assert command["type"] == "drain"
                    await socket.send_json({"type": "reply", "id": command["id"], "drained": False})
                    retired = await asyncio.wait_for(asyncio.shield(retiring), 2)
                    assert retired.phase == "retired" and provider.get(pod.id) is None
                    assert (await socket.receive_json(timeout=2))["type"] == "retired"
                finally:
                    await asyncio.wait_for(retiring, 12)

    asyncio.run(scenario())


def test_drained_worker_confirms_deletion_after_gateway_restart() -> None:
    class HeldDelete(Provider):
        hold = True

        def delete(self, pod_id: str) -> None:
            if self.hold:
                raise ProviderUnavailable("Fixture deletion is unconfirmed")
            super().delete(pod_id)

    async def scenario() -> None:
        provider, state = HeldDelete(), MemoryState()
        async with running(provider, state) as (gateway, client):
            scope = gateway.scope(KEYS[0].ss58_address)
            response = await request(client, KEYS[0], "POST", "/v1/pods/start", {"start_id": FIRST})
            assert response.status == 202
            await response.read()
            async with asyncio.timeout(2):
                while not gateway._pods:
                    await asyncio.sleep(0.01)
            pod = next(iter(gateway._pods.values()))
            async with client.ws_connect(f"/v1/control/{scope.hotkey}/{FIRST}", headers={
                "Authorization": "Bearer " + capability(gateway.secret, scope, FIRST), "x-witness-pod": pod.id,
            }) as socket:
                await socket.receive_json()
                await socket.send_json({"type": "drained"})
                async with asyncio.timeout(2):
                    while provider.pods[pod.id].lifecycle != "drained":
                        await asyncio.sleep(0.01)
                with pytest.raises(TimeoutError):
                    await socket.receive_json(timeout=0.05)
        async with running(provider, state) as (gateway, client):
            async with asyncio.timeout(2):
                while pod.id not in gateway._pods:
                    await asyncio.sleep(0.01)
            async with client.ws_connect(f"/v1/control/{scope.hotkey}/{FIRST}", headers={
                "Authorization": "Bearer " + capability(gateway.secret, scope, FIRST), "x-witness-pod": pod.id,
            }) as socket:
                assert (await socket.receive_json())["type"] == "drain"
                provider.hold = False
                await socket.send_json({"type": "drained"})
                assert (await socket.receive_json(timeout=2))["type"] == "retired"
            assert provider.get(pod.id) is None and provider.creates == 1

    asyncio.run(scenario())


def test_start_waits_for_worker_readiness_without_blocking_other_miners() -> None:
    async def scenario() -> None:
        provider = PausedProvider()
        async with running(provider, MemoryState(), poll_seconds=30) as (gateway, client):
            gateway.max_requests = 1
            allocation = asyncio.create_task(request(client, KEYS[0], "POST", "/v1/pods/start",
                                                     {"start_id": FIRST}, wait=20))
            scope = gateway.scope(KEYS[0].ss58_address)
            async with asyncio.timeout(2):
                while scope not in gateway._current or not gateway._pods:
                    await asyncio.sleep(0.01)
            assert not allocation.done()
            response = await request(client, KEYS[1], "GET", "/v1/pods/current")
            assert response.status == 200 and (await response.json())["session"] is None
            response = await asyncio.wait_for(request(client, KEYS[1], "POST", "/v1/pods/start",
                                                      {"start_id": SECOND}, wait=1), 2)
            assert response.status == 202 and (await response.json())["session"]["start_id"] == SECOND
            assert (await client.get("/health")).status == 200
            pod = next(iter(provider.pods.values()))
            async with client.ws_connect(f"/v1/control/{scope.hotkey}/{FIRST}", headers={
                "Authorization": "Bearer " + capability(gateway.secret, scope, FIRST), "x-witness-pod": pod.id,
            }) as socket:
                assert (await socket.receive_json())["type"] == "activate"
                await socket.send_json({"type": "ready", "ready": False, "certificate": "offline-ca",
                                        "proof_key_sha256": gateway.proof_hash, "pricing_sha256": gateway.pricing_hash})
                await asyncio.sleep(0.05)
                assert not allocation.done()
                await socket.send_json({"type": "ready", "ready": True, "certificate": "offline-ca",
                                        "proof_key_sha256": gateway.proof_hash, "pricing_sha256": gateway.pricing_hash})
                response = await asyncio.wait_for(allocation, 2)
                ready = await response.json()
                assert response.status == 200 and ready["ready"] is True
                assert ready["witness"]["pod_id"] == pod.id
                response = await request(client, KEYS[0], "GET", "/v1/pods/current", wait=20)
                assert (await response.json())["witness"]["pod_id"] == pod.id
            assert provider.creates == 1

    asyncio.run(scenario())


def test_timed_out_start_wait_resumes_the_same_pod() -> None:
    async def scenario() -> None:
        provider = PausedProvider()
        async with running(provider, MemoryState(), poll_seconds=30) as (gateway, client):
            value: dict[str, object] = {"start_id": FIRST}
            response = await asyncio.wait_for(request(client, KEYS[0], "POST", "/v1/pods/start", value, wait=1), 2)
            waiting = await response.json()
            assert response.status == 202 and waiting["ready"] is False
            assert waiting["retry_after"] == 0
            pod = next(iter(provider.pods.values()))
            scope = gateway.scope(KEYS[0].ss58_address)
            gateway.registry.peers[pod.id] = Peer(cast(web.WebSocketResponse, SimpleNamespace(closed=False)),
                                                 scope, FIRST, pod.id, ready=True, certificate="offline-ca")
            response = await asyncio.wait_for(request(client, KEYS[0], "POST", "/v1/pods/start", value, wait=1), 2)
            assert response.status == 200 and (await response.json())["witness"]["pod_id"] == pod.id
            assert provider.creates == 1

    asyncio.run(scenario())


def test_connection_does_not_reissue_access_to_a_revoked_pod() -> None:
    async def scenario() -> None:
        provider = PausedProvider()
        async with running(provider, MemoryState(), poll_seconds=30) as (gateway, client):
            response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                     {"start_id": FIRST})
            assert response.status == 202
            await response.read()
            scope = gateway.scope(KEYS[0].ss58_address)
            async with asyncio.timeout(2):
                while scope not in gateway._current or not gateway._pods:
                    await asyncio.sleep(0.01)
            pod = next(iter(provider.pods.values()))
            gateway.registry.peers[pod.id] = Peer(cast(web.WebSocketResponse, SimpleNamespace(closed=False)),
                                                 scope, FIRST, pod.id, ready=True, certificate="offline-ca")
            response = await request(client, KEYS[0], "GET", "/v1/pods/current", wait=1)
            assert (await response.json())["witness"]["pod_id"] == pod.id
            assert gateway.allocator.selection is not None
            gateway.allocator.selection.update(pod, active=False)
            response = await asyncio.wait_for(request(client, KEYS[0], "GET", "/v1/pods/current", wait=1), 2)
            result = await response.json()
            assert result["ready"] is False and result["witness"] is None
            assert provider.creates == 1

    asyncio.run(scenario())


def test_retries_from_multiple_clients_preserve_fifo_and_create_one_pod_each() -> None:
    async def scenario() -> None:
        provider = PausedProvider()
        provider.pause = "create"
        provider.release.clear()
        async with running(provider, MemoryState()) as (_, client):
            response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                     {"start_id": FIRST})
            assert response.status == 202
            await response.read()
            async with asyncio.timeout(2):
                while not provider.entered.is_set():
                    await asyncio.sleep(0.01)
            response = await request(client, KEYS[1], "POST", "/v1/pods/start",
                                     {"start_id": SECOND})
            assert response.status == 202
            await response.read()
            ids = [str(uuid.uuid4()) for _ in range(12)]
            retries = await asyncio.gather(*(request(client, KEYS[0], "POST", "/v1/pods/start",
                {"start_id": start_id}) for start_id in ids))
            joined = []
            for start_id, response in zip(ids, retries, strict=True):
                assert response.status in (202, 429)
                reply = await response.json()
                if response.status == 202:
                    assert reply["session"]["start_id"] == FIRST
                    joined.append(start_id)
            provider.release.set()
            async with asyncio.timeout(12):
                while provider.creates != 2:
                    await asyncio.sleep(0.01)
            assert [pod.markers["WITNESS_HOTKEY"] for pod in provider.pods.values()] == [key.ss58_address for key in KEYS]
            assert joined
            for start_id in joined:
                response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                         {"start_id": start_id})
                assert (await response.json())["session"]["start_id"] == FIRST
            for _ in range(3):
                response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                         {"start_id": FIRST})
                assert (await response.json())["session"]["start_id"] == FIRST
            assert provider.creates == 2

    asyncio.run(scenario())


def test_a_lost_pod_can_be_replaced_at_the_next_global_create_slot() -> None:
    async def scenario() -> None:
        provider, clock = Provider(), [1000.0]
        async with running(provider, MemoryState(), clock=clock) as (_, client):
            response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                     {"start_id": FIRST})
            assert response.status == 202
            await response.read()
            async with asyncio.timeout(2):
                while True:
                    response = await request(client, KEYS[0], "GET", "/v1/pods/current")
                    if (await response.json())["session"]["phase"] == "allocated":
                        break
                    await asyncio.sleep(0.01)
            provider.delete(next(iter(provider.pods)))
            async with asyncio.timeout(2):
                while True:
                    response = await request(client, KEYS[0], "GET", "/v1/pods/current")
                    if (await response.json())["session"]["phase"] == "lost":
                        break
                    await asyncio.sleep(0.01)
            response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                     {"start_id": SECOND, "replaces": FIRST})
            reply = await response.json()
            assert reply["session"]["phase"] == "queued" and reply["next_eligible_at"] == 1000
            await asyncio.sleep(0.05)
            assert provider.creates == 1
            clock[0] = 1010
            async with asyncio.timeout(2):
                while provider.creates != 2:
                    await asyncio.sleep(0.01)

    asyncio.run(scenario())


def test_new_requests_replace_running_pods_and_old_delivery_retries_survive_restart() -> None:
    async def scenario() -> None:
        provider, state = Provider(), MemoryState()
        async with running(provider, state, create_interval=0) as (_, client):
            for start_id in (FIRST, SECOND):
                response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                         {"start_id": start_id})
                assert response.status == 202 and (await response.json())["session"]["start_id"] == start_id
                async with asyncio.timeout(2):
                    while True:
                        response = await request(client, KEYS[0], "GET", f"/v1/pods/{start_id}")
                        if (await response.json())["session"]["phase"] == "allocated":
                            break
                        await asyncio.sleep(0.01)
            assert provider.creates == 2 and len(provider.pods) == 1
            assert next(iter(provider.pods.values())).markers["WITNESS_START_ID"] == SECOND
        async with running(provider, state, create_interval=0) as (_, client):
            response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                     {"start_id": FIRST})
            reply = await response.json()
            assert response.status == 202 and reply["session"]["start_id"] == FIRST
            assert reply["session"]["phase"] == "lost"
            response = await request(client, KEYS[0], "GET", f"/v1/pods/{FIRST}")
            assert response.status == 200 and (await response.json())["session"]["phase"] == "lost"
            response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                     {"start_id": SECOND})
            assert response.status == 200 and (await response.json())["session"]["start_id"] == SECOND
            assert provider.creates == 2 and len(provider.pods) == 1

    asyncio.run(scenario())


def test_a_previous_round_pod_releases_capacity_for_the_same_miner() -> None:
    async def scenario() -> None:
        provider, clock = Provider(), [1000.0]
        async with coordinator(provider, MemoryState(), clock) as gateway:
            gateway.allocator.admission = Admission(max_pods=1)
            scope = gateway.scope(KEYS[0].ss58_address)
            old = provider.create(gateway.allocator.spec.body(replace(scope, round="test-0"), FIRST))
            gateway._publish(provider.list())
            gateway.queue.enqueue(scope, SECOND, None)
            await gateway._step()
            assert old.id not in provider.pods
            assert [pod.scope() for pod in provider.pods.values()] == [scope]

    asyncio.run(scenario())


def test_replacement_keeps_fifo_while_old_pod_deletion_is_unconfirmed() -> None:
    class SlowDelete(Provider):
        hold = True

        def delete(self, pod_id: str) -> None:
            if not self.hold:
                super().delete(pod_id)

    async def scenario() -> None:
        provider, clock = SlowDelete(), [1000.0]
        async with coordinator(provider, MemoryState(), clock) as gateway:
            first, second = (gateway.scope(key.ss58_address) for key in KEYS)
            old = provider.create(gateway.allocator.spec.body(first, FIRST))
            gateway._publish(provider.list())
            gateway.queue.enqueue(first, SECOND, None)
            gateway.queue.enqueue(second, FIRST, None)
            await gateway._step()
            clock[0] += 1
            gateway._publish(provider.list())
            await gateway._step()
            assert provider.creates == 1 and old.id in provider.pods
            provider.hold = False
            clock[0] = 1030
            gateway._publish(provider.list())
            await gateway._step()
            assert old.id not in provider.pods and provider.creates == 2
            clock[0] += 10
            gateway._publish(provider.list())
            await gateway._step()
            assert [pod.scope() for pod in provider.pods.values()] == [first, second]

    asyncio.run(scenario())


def test_a_stalled_request_body_times_out_without_blocking_other_miners() -> None:
    async def partial_body() -> AsyncIterator[bytes]:
        yield b"{"
        await asyncio.sleep(1)
        yield b"}"

    async def scenario() -> None:
        provider = Provider()
        async with running(provider, MemoryState()) as (_, client):
            headers = hotkey_headers(KEYS[0], b"{}", method="POST", path="/v1/pods/start")
            async with asyncio.timeout(0.5):
                response = await client.post("/v1/pods/start", data=partial_body(), headers=headers)
            assert response.status == 408
            assert response.headers["Connection"] == "close"
            assert (await request(client, KEYS[1], "GET", "/v1/pods/current")).status == 200
            assert (await client.get("/health")).status == 200
            assert provider.creates == 0

    asyncio.run(scenario())


def test_gateway_restart_retains_an_unknown_creation_without_replaying_it() -> None:
    class Unknown(Provider):
        def create(self, body: dict[str, object]) -> Pod:
            self.creates += 1
            raise CreateUnknown("Unknown result")

    async def scenario() -> None:
        state, provider, clock = MemoryState(), Unknown(), [1000.0]
        async with running(provider, state, clock=clock) as (_, client):
            response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                     {"start_id": FIRST})
            assert response.status == 202
            await response.read()
            async with asyncio.timeout(2):
                while provider.creates != 1:
                    await asyncio.sleep(0.01)
        clock[0] += 3600
        async with running(provider, state, clock=clock) as (_, client):
            response = await request(client, KEYS[0], "POST", "/v1/pods/start",
                                     {"start_id": SECOND})
            assert (await response.json())["session"]["start_id"] == FIRST
            await asyncio.sleep(0.05)
            assert provider.creates == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("submitted", [False, True])
def test_removed_members_do_not_block_other_miners_or_release_unknown_creates(
    submitted: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        provider, clock = Provider(), [1000.0]
        async with coordinator(provider, MemoryState(), clock) as gateway:
            first, second = (gateway.scope(key.ss58_address) for key in KEYS)
            gateway.queue.enqueue(first, FIRST, None)
            if submitted:
                gateway.queue.authorize(first, FIRST)
                provider.create(gateway.allocator.spec.body(first, FIRST))
                clock[0] += 10
            gateway.queue.enqueue(second, SECOND, None)
            snapshot = gateway.auth.membership.snapshot
            assert snapshot is not None
            inventory = provider.list

            def revoked_inventory(*, fresh: bool = False) -> list[Pod]:
                snapshot["hotkeys"] = [KEYS[1].ss58_address]
                return inventory(fresh=fresh)

            gateway._publish(provider.list())
            with monkeypatch.context() as patch:
                if submitted:
                    snapshot["hotkeys"] = [KEYS[1].ss58_address]
                else:
                    patch.setattr(provider, "list", revoked_inventory)
                await gateway._step()
            clock[0] += 1
            gateway._publish(provider.list())
            await gateway._step()
            assert any(pod.scope() == second for pod in provider.pods.values())
            assert (gateway.queue.pending(first) is not None) == submitted
            if submitted:
                assert (first, FIRST) in gateway.queue.unresolved()
            else:
                snapshot["hotkeys"] = [key.ss58_address for key in KEYS]
                clock[0] += 31
                gateway.queue.enqueue(first, str(uuid.uuid4()), None)
                gateway._publish(provider.list())
                await gateway._step()
                assert any(pod.scope() == first for pod in provider.pods.values())

    asyncio.run(scenario())


def test_backlog_allows_grants_renewal_and_retirement(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        provider, clock = Provider(), [1000.0]
        keys = tuple(Keypair.create_from_uri(f"//BacklogMiner{number}") for number in range(64))
        epoch = time.time() - clock[0]
        monkeypatch.setattr(time, "time", lambda: epoch + clock[0])
        async with coordinator(provider, MemoryState(), clock, keys=keys) as gateway:
            for key in keys:
                gateway.queue.enqueue(gateway.scope(key.ss58_address), str(uuid.uuid4()), None)
            for _ in range(120):
                gateway._publish(provider.list())
                await gateway._step()
                gateway._publish(provider.list())
                for pod in provider.pods.values():
                    if pod.id not in gateway.registry.peers:
                        scope = pod.scope()
                        assert scope is not None
                        gateway.registry.peers[pod.id] = Peer(
                            cast(web.WebSocketResponse, SimpleNamespace(closed=False)), scope,
                            pod.markers["WITNESS_START_ID"], pod.id, ready=True, certificate="offline-ca")
                clock[0] += 1
            first = gateway.scope(keys[0].ss58_address)
            endpoint = await gateway._endpoint(first, gateway._cached(first))
            assert endpoint is not None and gateway.queue.entries()
            expires = endpoint["expires"]
            clock[0] += 600
            await gateway._step()
            renewed = await gateway._endpoint(first, gateway._cached(first))
            assert renewed is not None and int(str(renewed["expires"])) > int(str(expires))
            second = list(provider.pods.values())[1]
            provider.pods[second.id] = replace(second, status="EXITED")
            gateway._publish(provider.list())
            clock[0] += 31
            await gateway._step()
            assert second.id not in provider.pods and gateway.queue.entries()

    asyncio.run(scenario())


def test_routine_fence_refresh_keeps_admission_available_but_expired_samples_fail_closed() -> None:
    async def scenario() -> None:
        entered, release = threading.Event(), threading.Event()
        calls = 0

        def fence() -> bool:
            nonlocal calls
            calls += 1
            if calls > 1:
                entered.set()
                assert release.wait(3)
            return True

        async with coordinator(Provider(), MemoryState(), [1000.0]) as gateway:
            gateway.fence = fence
            task = asyncio.create_task(gateway._fences())

            async def status() -> web.StreamResponse:
                incoming = make_mocked_request("GET", "/v1/pods/current", headers=hotkey_headers(
                    KEYS[0], b"", method="GET", path="/v1/pods/current"))
                incoming._read_bytes = b""
                return await gateway.middleware(incoming, gateway.current)

            try:
                async with asyncio.timeout(4):
                    while not entered.is_set():
                        await asyncio.sleep(0.005)
                await asyncio.sleep(0.25)
                assert (await status()).status == 200
                gateway._fence_at = time.monotonic() - 5
                assert (await status()).status == 503
            finally:
                release.set()
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())


def test_a_rejected_create_completes_after_a_transient_journal_failure() -> None:
    class Rejected(Provider):
        def create(self, body: dict[str, object]) -> Pod:
            self.creates += 1
            raise CreateRejected("No stock", status=429)

    class State(MemoryState):
        fail = True

        def save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
            if key.startswith("pod-admission/") and not json.loads(data)["pending"] and self.fail:
                self.fail = False
                raise WriteUncertain("Transient timeout; completion was not applied")
            return super().save(key, data, expected)

    async def scenario() -> None:
        provider, state, clock = Rejected(), State(), [1000.0]
        async with coordinator(provider, state, clock) as gateway:
            scope = gateway.scope(KEYS[0].ss58_address)
            gateway.queue.enqueue(scope, FIRST, None)
            with pytest.raises(WriteUncertain):
                await gateway._step()
            gateway.queue.load()
            gateway._publish(provider.list())
            clock[0] += 31
            await gateway._step()
            assert gateway.queue.pending(scope) is None and provider.creates == 1
            result = gateway._cached(scope)
            assert result is not None and result.phase == "rejected"
            clock[0] += 1800
            gateway.queue.enqueue(scope, SECOND, FIRST)
            await gateway._step()
            assert provider.creates == 2 and gateway.queue.pending(scope) is None

    asyncio.run(scenario())


def test_a_full_fleet_and_queue_keep_the_event_loop_responsive() -> None:
    async def scenario() -> None:
        provider, state, clock = Provider(), MemoryState(), [1000.0]
        keys = tuple(Keypair(public_key=number.to_bytes(32, "big").hex()) for number in range(1, 1275))
        async with coordinator(provider, state, clock, keys=keys) as gateway:
            for key in keys[:250]:
                scope = gateway.scope(key.ss58_address)
                pod = provider.create(gateway.allocator.spec.body(scope, FIRST))
                provider.pods[pod.id] = replace(pod, name=scope.name(FIRST) + "-active")
            queued = QueueState(deployment="test", sequence=1024, pending={
                key.ss58_address: PendingStart(round="test-1", start_id=FIRST, sequence=number, eligible_at=1000)
                for number, key in enumerate(keys[250:])
            })
            state.save(gateway.queue.key, queued.model_dump_json().encode(), None)
            gateway.queue.load()
            gateway._publish(provider.list())
            started = time.monotonic()

            async def heartbeat() -> float:
                await asyncio.sleep(0)
                return time.monotonic() - started

            tick = asyncio.create_task(heartbeat())
            await gateway._step()
            assert await tick < 1
            assert len(provider.pods) == 250 and len(gateway.queue.entries()) == 1024

    asyncio.run(scenario())


def test_global_creation_pacing_preserves_fifo_when_a_new_miner_arrives() -> None:
    async def scenario() -> None:
        provider, clock = Provider(), [1000.0]
        keys = (*KEYS, Keypair.create_from_uri("//Charlie"))
        async with coordinator(provider, MemoryState(), clock, keys=keys) as gateway:
            first, second, third = (gateway.scope(key.ss58_address) for key in keys)
            gateway.queue.enqueue(first, FIRST, None)
            gateway.queue.enqueue(second, SECOND, None)
            await gateway._step()
            gateway._publish(provider.list())
            clock[0] = 1001
            await gateway._step()
            clock[0] = 1002
            gateway.queue.enqueue(third, str(uuid.uuid4()), None)
            clock[0] = 1010
            await gateway._step()
            assert [pod.scope() for pod in provider.pods.values()] == [first, second]
            gateway._publish(provider.list())
            clock[0] = 1020
            await gateway._step()
            assert [pod.scope() for pod in provider.pods.values()] == [first, second, third]

    asyncio.run(scenario())


def test_a_lost_cancellation_acknowledgement_allows_a_new_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    class State(MemoryState):
        lose_ack = False

        def save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
            saved = super().save(key, data, expected)
            if self.lose_ack and key.startswith("pod-admission/") and not json.loads(data)["pending"]:
                self.lose_ack = False
                raise WriteUncertain("Cancellation applied; acknowledgement lost")
            return saved

    async def scenario() -> None:
        provider, state, clock = Provider(), State(), [1000.0]
        async with coordinator(provider, state, clock) as gateway:
            scope = gateway.scope(KEYS[0].ss58_address)
            gateway.queue.enqueue(scope, FIRST, None)
            snapshot = gateway.auth.membership.snapshot
            assert snapshot is not None
            inventory = provider.list

            def revoked_inventory(*, fresh: bool = False) -> list[Pod]:
                snapshot["hotkeys"] = []
                return inventory(fresh=fresh)

            state.lose_ack = True
            with monkeypatch.context() as patch:
                patch.setattr(provider, "list", revoked_inventory)
                with pytest.raises(WriteUncertain):
                    await gateway._step()
            gateway.queue.load()
            assert gateway.queue.pending(scope) is None
            snapshot["hotkeys"] = [key.ss58_address for key in KEYS]
            clock[0] += 31
            gateway.queue.enqueue(scope, SECOND, None)
            await gateway._step()
            assert [pod.markers["WITNESS_START_ID"] for pod in provider.pods.values()] == [SECOND]
            assert gateway.queue.pending(scope) is None

    asyncio.run(scenario())

"""Explicit async Pod replacement joins allocation and retains saved results."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from bittensor_wallet import Keypair
from test_async_client import isolated_async_client as isolated_async_client
from test_async_transport import tls_context as tls_context
from test_config import config as config
from witness_client import AsyncWitnessClient, WitnessConfig, WitnessError

PNG = b"\x89PNG\r\n\x1a\n"
FIRST = "00000000-0000-4000-8000-000000000001"


class AllocationService:
    def __init__(self, certificate: str) -> None:
        self.certificate = certificate
        self.session: dict[str, object] | None = {
            "start_id": FIRST, "pod_id": "pod-0", "phase": "allocated",
        }
        self.history: dict[str, dict[str, object]] = {}
        self.created = 0
        self.saved: dict[str, dict[str, object]] = {}
        self.save_requests = 0
        self.connections: list[httpx.AsyncClient] = []
        self.drop_reply = False
        self.block_before_start = self.block_after_start = False
        self.start_pending = asyncio.Event()
        self.release_start = asyncio.Event()
        self.ready = True
        self.reuse_physical_pod = False
        self.reject_start = False
        self.retiring_polls = 0

    def reply(self, session: dict[str, object] | None) -> dict[str, object]:
        result: dict[str, object] = {"round": "test-0", "session": session, "retry_after": 0.001}
        if session is not None and session["phase"] == "allocated":
            result["witness"] = {
                "start_id": session["start_id"], "pod_id": session["pod_id"],
                "address": f"192.0.2.{self.created + 1}", "port": 443,
                "server_name": "witness.internal", "expires": int(time.time()) + 3600,
                "access": "scoped-access", "certificate": self.certificate,
            }
        return result

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        if request.url.host == "witness.test":
            if request.method == "POST":
                assert request.url.path == "/v1/pods/start"
                if self.block_before_start:
                    self.start_pending.set()
                    await self.release_start.wait()
                value = json.loads(request.content)
                identifier = value["start_id"]
                if identifier not in self.history:
                    self.created += 1
                    self.session = {
                        "start_id": identifier, "replaces": value["replaces"],
                        "pod_id": "pod-0" if self.reuse_physical_pod else f"pod-{self.created}",
                        "phase": "rejected" if self.reject_start else "allocated" if self.ready else "queued",
                    }
                    self.history[identifier] = self.session
                if self.block_after_start:
                    self.start_pending.set()
                    await self.release_start.wait()
                if self.drop_reply:
                    self.drop_reply = False
                    raise httpx.ReadError("lost allocation acknowledgement", request=request)
                return httpx.Response(200, json=self.reply(self.history[identifier]))
            if request.url.path != "/v1/pods/current":
                assert self.session is not None
                assert request.url.path == f"/v1/pods/{self.session['start_id']}"
                if self.ready:
                    self.session["phase"] = "allocated"
            elif self.session is not None and self.session["phase"] == "retiring":
                self.retiring_polls += 1
                if self.retiring_polls > 1:
                    self.session["phase"] = "retired"
            return httpx.Response(200, json=self.reply(self.session))

        assert request.extensions["sni_hostname"] == "witness.internal"
        assert request.url.host == f"192.0.2.{self.created + 1}"
        assert self.session is not None
        if request.url.path == "/v1/render/grid":
            return httpx.Response(
                200, content=PNG + str(self.session["pod_id"]).encode(),
                headers={"Content-Type": "image/png"},
            )
        assert request.url.path == "/v1/submissions"
        if request.method == "POST":
            self.save_requests += 1
            value = json.loads(request.content)
            result: dict[str, object] = {"js": "result.js", "size": len(value["source"])}
            self.saved[hashlib.sha256(b"image").hexdigest()] = result
            return httpx.Response(200, json={"submission": result})
        return httpx.Response(200, json={"submissions": self.saved})


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch, tls_context: tuple[object, Path]) -> AllocationService:
    realm = AllocationService(tls_context[1].read_text())
    http_type = httpx.AsyncClient

    def http_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(realm.handle)
        http = http_type(*args, **kwargs)
        realm.connections.append(http)
        return http

    monkeypatch.setattr("witness_client.async_pods.httpx.AsyncClient", http_client)
    monkeypatch.setattr("witness_client.delivery.random.uniform", lambda _low, _high: 0.001)
    return realm


def client(config: WitnessConfig, **kwargs: Any) -> AsyncWitnessClient:
    return AsyncWitnessClient(config, hotkey=Keypair.create_from_uri("//Alice"), openai_api_key="x" * 80, **kwargs)


@pytest.mark.parametrize("phase", [None, "allocated", "lost", "retired", "rejected"])
def test_replace_as_first_operation_creates_only_one_pod(
    config: WitnessConfig, service: AllocationService, phase: str | None,
) -> None:
    service.session = None if phase is None else {"start_id": FIRST, "pod_id": "pod-0", "phase": phase}

    async def run() -> None:
        async with client(config) as witness:
            await witness.replace_pod()
            assert await witness.render("source") == PNG + b"pod-1"
            assert service.created == 1
        assert all(http.is_closed for http in service.connections)
        assert not Path(config["state_directory"]).exists()

    asyncio.run(run())


@pytest.mark.parametrize("connected", [False, True])
def test_concurrent_replacements_join_but_a_later_call_replaces_again(
    config: WitnessConfig, service: AllocationService, connected: bool,
) -> None:
    async def run() -> None:
        async with client(config) as witness:
            if connected:
                await witness.get_submitted()
            await asyncio.gather(*(witness.replace_pod() for _ in range(8)))
            assert await witness.render("source") == PNG + b"pod-1"
            assert service.created == 1
            await witness.replace_pod()
            assert await witness.render("source") == PNG + b"pod-2"
            assert service.created == 2

    asyncio.run(run())


def test_clients_replacing_the_same_observed_pod_share_saved_results(
    config: WitnessConfig, service: AllocationService,
) -> None:
    async def run() -> None:
        async with client(config) as first, client(config) as second:
            await asyncio.gather(first.get_submitted(), second.get_submitted())
            saved = await first.submit(b"image", "source")
            await asyncio.gather(first.replace_pod(), second.replace_pod())
            assert await first.render("source") == await second.render("source") == PNG + b"pod-1"
            assert list((await second.get_submitted()).values()) == [saved]
            assert service.created == service.save_requests == 1

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["queued", "draining", "creating"])
def test_replace_joins_an_existing_allocation(
    config: WitnessConfig, service: AllocationService, phase: str,
) -> None:
    service.session = {"start_id": FIRST, "pod_id": "pod-0", "phase": phase}

    async def run() -> None:
        async with client(config) as witness:
            await witness.replace_pod()
            assert await witness.render("source") == PNG + b"pod-0"
            assert service.created == 0

    asyncio.run(run())


def test_replace_waits_for_retirement_before_creating_one_pod(
    config: WitnessConfig, service: AllocationService,
) -> None:
    service.session = {"start_id": FIRST, "pod_id": "pod-0", "phase": "retiring"}

    async def run() -> None:
        async with client(config) as witness:
            await witness.replace_pod()
            assert await witness.render("source") == PNG + b"pod-1"
            assert service.created == 1

    asyncio.run(run())


def test_lost_allocation_acknowledgement_does_not_create_another_pod(
    config: WitnessConfig, service: AllocationService,
) -> None:
    service.drop_reply = True

    async def run() -> None:
        async with client(config) as witness:
            await witness.replace_pod()
            assert await witness.render("source") == PNG + b"pod-1"
            assert service.created == 1

    asyncio.run(run())


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("connected", [False, True])
def test_cancelled_replacement_resumes_with_the_next_operation(
    config: WitnessConfig, service: AllocationService, accepted: bool, connected: bool,
) -> None:
    service.block_before_start, service.block_after_start = not accepted, accepted

    async def run() -> None:
        async with client(config) as witness:
            if connected:
                await witness.get_submitted()
            task = asyncio.create_task(witness.replace_pod())
            await asyncio.wait_for(service.start_pending.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            service.release_start.set()
            assert await witness.render("source") == PNG + b"pod-1"
            assert service.created == 1

    asyncio.run(run())


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("initial", ["absent", "unconnected", "connected"])
def test_readiness_timeout_resumes_the_pending_replacement(
    config: WitnessConfig, service: AllocationService, restart: bool, initial: str,
) -> None:
    if initial == "absent":
        service.session = None

    async def run() -> None:
        async with client(config, delivery_timeout=0.05) as witness:
            if initial == "connected":
                await witness.get_submitted()
            service.ready = False
            with pytest.raises(WitnessError) as failed:
                await witness.replace_pod()
            assert failed.value.code in ("pod_provisioning_pending", "capture_timeout")
            service.ready = True
            if not restart:
                await witness.replace_pod()
                assert await witness.render("source") == PNG + b"pod-1"
        if restart:
            async with client(config) as witness:
                await witness.replace_pod()
                assert await witness.render("source") == PNG + b"pod-1"
        assert service.created == 1

    asyncio.run(run())


def test_replacement_cannot_return_the_same_physical_pod(
    config: WitnessConfig, service: AllocationService,
) -> None:
    service.reuse_physical_pod = True

    async def run() -> None:
        async with client(config) as witness:
            with pytest.raises(WitnessError) as failed:
                await witness.replace_pod()
            assert failed.value.code == "pod_replacement_unconfirmed"
            assert service.created == 1

    asyncio.run(run())


def test_rejected_replacement_raises_without_starting_again(
    config: WitnessConfig, service: AllocationService,
) -> None:
    service.reject_start = True

    async def run() -> None:
        async with client(config) as witness:
            with pytest.raises(WitnessError) as failed:
                await witness.replace_pod()
            assert failed.value.code == "pod_unavailable"
            assert service.created == 1

    asyncio.run(run())


def test_repeated_replacements_close_idle_clients_and_keep_the_current_client_open(
    config: WitnessConfig, service: AllocationService,
) -> None:
    async def run() -> None:
        async with client(config) as witness:
            await witness.get_submitted()
            for number in range(1, 5):
                old = witness._gateway._direct
                assert old is not None
                await witness.replace_pod()
                assert await witness.render("source") == PNG + f"pod-{number}".encode()
                assert old.http.is_closed
                current = witness._gateway._direct
                assert current is not None and not current.http.is_closed
                assert witness._gateway._connections == [current.http]
        assert all(http.is_closed for http in service.connections)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["submission", "call", "tunnel"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_old_requests_drain_without_blocking_the_new_client(
    config: WitnessConfig, service: AllocationService, monkeypatch: pytest.MonkeyPatch,
    operation: str, outcome: str,
) -> None:
    async def run() -> None:
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        handle = service.handle

        async def finish() -> None:
            entered.set()
            await release.wait()
            if outcome == "error":
                raise httpx.ConnectError("Fixture old connection failed")

        async def deliver(request: httpx.Request) -> httpx.Response:
            if request.url.host == "192.0.2.1":
                if operation == "submission" and request.method == "POST":
                    await finish()
                    return httpx.Response(200, json={"submission": {"js": "saved.js", "views": "saved"}})
                if operation == "call" and request.url.path == "/v1/calls/fixture":
                    await finish()
                    return httpx.Response(200, json={"saved": True})
            return await handle(request)

        @asynccontextmanager
        async def socket(*args: object, **kwargs: object) -> AsyncIterator[SimpleNamespace]:
            yield SimpleNamespace()

        monkeypatch.setattr(service, "handle", deliver)
        monkeypatch.setattr("witness_client.async_pods.connect", socket)
        async with client(config) as witness:
            await witness.get_submitted()
            old = witness._gateway._direct
            assert old is not None
            close = old.http.aclose

            async def close_old() -> None:
                await close()
                closed.set()

            monkeypatch.setattr(old.http, "aclose", close_old)

            async def old_request() -> object:
                if operation == "submission":
                    return await witness.submit(b"image", "source")
                if operation == "call":
                    return await witness._gateway._request_once("GET", "/v1/calls/fixture")
                async with witness._gateway.tunnel("fixture"):
                    await finish()
                return None

            pending = asyncio.create_task(old_request())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.wait_for(witness.replace_pod(), 1)
                assert await asyncio.wait_for(witness.render("source"), 1) == PNG + b"pod-1"
                current = witness._gateway._direct
                assert current is not None and not current.http.is_closed
                assert not old.http.is_closed and not pending.done()
                if outcome == "cancel":
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                else:
                    release.set()
                    if outcome == "error":
                        with pytest.raises((httpx.ConnectError, WitnessError)):
                            await pending
                    else:
                        await pending
                await asyncio.wait_for(closed.wait(), 1)
                assert witness._gateway._direct is current
                assert not current.http.is_closed
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert all(http.is_closed for http in service.connections)

    asyncio.run(run())


def test_slow_client_close_does_not_hold_the_pod_lock_or_block_new_requests(
    config: WitnessConfig, service: AllocationService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        closing, release = asyncio.Event(), asyncio.Event()
        async with client(config) as witness:
            await witness.get_submitted()
            old = witness._gateway._direct
            assert old is not None
            close = old.http.aclose

            async def close_old() -> None:
                closing.set()
                await release.wait()
                await close()

            monkeypatch.setattr(old.http, "aclose", close_old)
            try:
                await asyncio.wait_for(witness.replace_pod(), 1)
                await asyncio.wait_for(closing.wait(), 1)
                assert not witness._gateway._pod_lock.locked()
                assert await asyncio.wait_for(witness.render("source"), 1) == PNG + b"pod-1"
            finally:
                release.set()
        assert all(http.is_closed for http in service.connections)

    asyncio.run(run())


def test_old_client_stays_open_until_its_last_request_finishes(
    config: WitnessConfig, service: AllocationService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        closed = asyncio.Event()
        handle = service.handle

        async def deliver(request: httpx.Request) -> httpx.Response:
            if request.url.host == "192.0.2.1" and request.method == "POST":
                number = int(json.loads(request.content)["source"])
                entered[number].set()
                await release[number].wait()
                return httpx.Response(200, json={"submission": {"js": "saved.js", "views": "saved"}})
            return await handle(request)

        monkeypatch.setattr(service, "handle", deliver)
        async with client(config) as witness:
            await witness.get_submitted()
            old = witness._gateway._direct
            assert old is not None
            close = old.http.aclose

            async def close_old() -> None:
                await close()
                closed.set()

            monkeypatch.setattr(old.http, "aclose", close_old)
            pending = [asyncio.create_task(witness.submit(b"image", str(number))) for number in range(2)]
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 1)
                await witness.replace_pod()
                release[0].set()
                await pending[0]
                assert not old.http.is_closed
                assert not pending[1].done()
                assert await witness.render("source") == PNG + b"pod-1"
                release[1].set()
                await pending[1]
                await asyncio.wait_for(closed.wait(), 1)
            finally:
                for event in release:
                    event.set()
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(run())


def test_failed_background_close_does_not_block_new_work_and_is_reported_at_shutdown(
    config: WitnessConfig, service: AllocationService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        witness = client(config)
        await witness.__aenter__()
        await witness.get_submitted()
        old = witness._gateway._direct
        assert old is not None
        close = old.http.aclose

        async def unavailable() -> None:
            raise RuntimeError("Fixture connection cleanup failed")

        monkeypatch.setattr(old.http, "aclose", unavailable)
        try:
            await witness.replace_pod()
            assert await witness.render("source") == PNG + b"pod-1"
            current = witness._gateway._direct
            assert current is not None and not current.http.is_closed
            with pytest.raises(RuntimeError, match="Fixture connection cleanup failed"):
                await witness.aclose()
            assert current.http.is_closed
            assert witness._http.is_closed and witness._gateway._artifacts.http.is_closed
        finally:
            await close()

    asyncio.run(run())

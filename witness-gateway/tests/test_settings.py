import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from aiohttp import web
from bittensor_wallet import Keypair
from pod_gateway.dependencies import gateway_spec, worker_spec
from pod_gateway.membership import Membership
from pod_gateway.settings import GatewaySettings, Settings
from pydantic import SecretStr
from subnet_common.witness.auth import Authenticator, AuthRejected
from subnet_common.witness.models import Scope
from subnet_common.witness.pricing import PricingStore
from support import MemoryState
from test_recording import PRICING
from witness_client.auth import hotkey_headers


@pytest.mark.parametrize("round_number", ["test-0", "test-1", "test-2"])
def test_test_hotkey_can_authenticate_without_registration(round_number: str) -> None:
    hotkey = Keypair.create_from_mnemonic(Keypair.generate_mnemonic()).ss58_address
    membership = Membership(17, round_number, hotkey)
    now = int(time.time() * 1000)
    membership.snapshot = {
        "version": 1,
        "network": "finney",
        "netuid": 17,
        "block": 1,
        "fetched_at": now,
        "block_time": now,
        "hotkeys": [],
    }
    membership.require(hotkey)
    with pytest.raises(AuthRejected, match="hotkey_not_registered"):
        membership.require("5" * 48)


def test_test_hotkey_is_rejected_for_competition_rounds() -> None:
    with pytest.raises(ValueError, match="Invalid membership configuration"):
        Membership(17, "56", "5" * 48)


@pytest.mark.parametrize("age_ns,accepted", [
    (-30_000_000_000, True), (30_000_000_000, True),
    (-30_000_000_001, False), (30_000_000_001, False),
])
def test_hotkey_timestamp_window(monkeypatch: pytest.MonkeyPatch, age_ns: int, accepted: bool) -> None:
    key = Keypair.create_from_uri("//Alice")
    clock = [time.time_ns()]
    monkeypatch.setattr("subnet_common.witness.auth.time.time_ns", lambda: clock[0])
    headers = hotkey_headers(key, b"", method="GET", path="/v1/pods/current")
    membership = Membership(17, "test-1")
    now = int(time.time() * 1000)
    membership.snapshot = {
        "version": 1, "network": "finney", "netuid": 17, "block": 1,
        "fetched_at": now, "block_time": now, "hotkeys": [key.ss58_address],
    }
    auth = Authenticator(membership.require)
    clock[0] += age_ns
    if accepted:
        assert auth.verify("GET", "/v1/pods/current", b"", headers) == key.ss58_address
    else:
        with pytest.raises(AuthRejected, match="stale_hotkey_auth"):
            auth.verify("GET", "/v1/pods/current", b"", headers)


@pytest.mark.parametrize("age_ns", [10_000_000_001, 20_000_000_000, 30_000_000_000])
def test_hotkey_replay_is_rejected_through_timestamp_window(monkeypatch: pytest.MonkeyPatch, age_ns: int) -> None:
    key = Keypair.create_from_uri("//Alice")
    clock = [time.time_ns()]
    monkeypatch.setattr("subnet_common.witness.auth.time.time_ns", lambda: clock[0])
    headers = hotkey_headers(key, b"", method="GET", path="/v1/pods/current")
    membership = Membership(17, "test-1")
    now = int(time.time() * 1000)
    membership.snapshot = {
        "version": 1, "network": "finney", "netuid": 17, "block": 1,
        "fetched_at": now, "block_time": now, "hotkeys": [key.ss58_address],
    }
    auth = Authenticator(membership.require)
    assert auth.verify("GET", "/v1/pods/current", b"", headers) == key.ss58_address
    clock[0] += age_ns
    with pytest.raises(AuthRejected, match="replayed_hotkey_auth"):
        auth.verify("GET", "/v1/pods/current", b"", headers)


def test_private_registry_can_launch_both_pod_roles(values: dict[str, object]) -> None:
    values["WITNESS_REGISTRY"] = "private-registry-id"
    settings = Settings.model_validate(values)
    scope = Scope("test", "test-1", settings.gateway_owner_hotkey)
    for build in (worker_spec, gateway_spec):
        body = build(settings).body(scope, "00000000-0000-4000-8000-000000000001")
        assert body["registry"] == "private-registry-id"
    assert gateway_spec(settings).env["WITNESS_REGISTRY"] == "private-registry-id"


def test_location_preferences_apply_to_both_pod_roles(values: dict[str, object]) -> None:
    values["WITNESS_DATA_CENTERS"] = "EU-CZ-1,EU-RO-1"
    settings = Settings.model_validate(values)
    scope = Scope("test", "test-1", settings.gateway_owner_hotkey)
    for build in (worker_spec, gateway_spec):
        body = build(settings).body(scope, "00000000-0000-4000-8000-000000000001")
        assert body["dataCenterIds"] == ["EU-CZ-1", "EU-RO-1"]


def test_gateway_admission_settings_are_bounded_and_forwarded(values: dict[str, object]) -> None:
    limits = {
        "GATEWAY_CREATE_INTERVAL_SECONDS": "20",
        "GATEWAY_REQUEST_INTERVAL_SECONDS": "15", "GATEWAY_POLL_SECONDS": "60",
        "GATEWAY_MAX_REQUESTS": "32", "GATEWAY_MAX_QUEUE": "512",
    }
    settings = Settings.model_validate({**values, **limits})
    assert {name: gateway_spec(settings).env[name] for name in limits} == limits
    for name in limits:
        with pytest.raises(ValueError):
            Settings.model_validate({**values, name: "0"})


@pytest.mark.parametrize("token", [None, ""])
def test_round_refresh_requires_a_github_token(values: dict[str, object], token: str | None) -> None:
    if token is None:
        values.pop("WITNESS_GITHUB_TOKEN")
    else:
        values["WITNESS_GITHUB_TOKEN"] = token
    with pytest.raises(ValueError, match="WITNESS_GITHUB_TOKEN"):
        Settings.model_validate(values)


def test_round_refresh_defaults_keep_policy_fresh_between_checks(values: dict[str, object]) -> None:
    settings = Settings.model_validate(values)
    assert settings.mode == "competition"
    assert settings.round_refresh_seconds == 60
    assert settings.round_policy_seconds == 120


def test_test_mode_configuration_survives_gateway_revival(values: dict[str, object]) -> None:
    values.update({
        "WITNESS_MODE": "test", "WITNESS_DEPLOYMENT": "witness-test-v1",
        "WITNESS_COMPETITION_BRANCH": "witness-test",
        "WITNESS_TEST_HOTKEY": Keypair.create_from_uri("//Alice").ss58_address,
    })
    settings = Settings.model_validate(values)
    revived = Settings.model_validate({**gateway_spec(settings).env, "WITNESS_DEPLOYMENT": settings.deployment})
    assert revived.mode == "test"
    assert revived.test_hotkey == values["WITNESS_TEST_HOTKEY"]
    assert revived.competition_branch == "witness-test"


@pytest.mark.parametrize("changes", [
    {"WITNESS_MODE": "competition"},
    {"WITNESS_TEST_HOTKEY": None},
    {"WITNESS_TEST_HOTKEY": "invalid"},
    {"WITNESS_DEPLOYMENT": "witness-public-v3"},
    {"WITNESS_COMPETITION_BRANCH": "main"},
])
def test_test_mode_requires_an_identity_and_separate_deployment(
    values: dict[str, object], changes: dict[str, object],
) -> None:
    values.update({
        "WITNESS_MODE": "test", "WITNESS_DEPLOYMENT": "witness-test-v1",
        "WITNESS_COMPETITION_BRANCH": "witness-test",
        "WITNESS_TEST_HOTKEY": Keypair.create_from_uri("//Alice").ss58_address,
    })
    with pytest.raises(ValueError):
        Settings.model_validate({**values, **changes})


@pytest.mark.parametrize("refresh,policy", [(60, 60), (61, 120)])
def test_round_refresh_rejects_intervals_outside_policy_bounds(
    values: dict[str, object], refresh: int, policy: int,
) -> None:
    with pytest.raises(ValueError):
        Settings.model_validate({**values, "WITNESS_ROUND_REFRESH_SECONDS": refresh,
                                 "WITNESS_ROUND_POLICY_SECONDS": policy})


@pytest.fixture
def values() -> dict[str, object]:
    return {
        "WITNESS_DEPLOYMENT": "competition",
        "WITNESS_ACTIVE_ROUND": "test-0",
        "WITNESS_ROUND_END": "2000000000",
        "WITNESS_GITHUB_TOKEN": "fixture-read-only-token",
        "WITNESS_GATEWAY_OWNER_HOTKEY": Keypair.create_from_mnemonic(Keypair.generate_mnemonic()).ss58_address,
        "WITNESS_GATEWAY_URL": "https://witness.test",
        "WITNESS_MASTER_KEY": "ab" * 32,
        "GATEWAY_RUNPOD_API_KEY": "fixture-runpod",
        "WITNESS_IMAGE": "registry/witness@sha256:" + "a" * 64,
        "WITNESS_GATEWAY_IMAGE": "registry/gateway@sha256:" + "b" * 64,
        "R2_ENDPOINT": "https://r2.test",
        "R2_ACCESS_KEY_ID": "fixture-id",
        "R2_SECRET_ACCESS_KEY": "fixture-secret",
        "R2_BUCKET": "fixture",
        "R2_REPLAY_BUCKET": "fixture-replays",
        "R2_REPLAY_CDN_URL": "https://replays.test",
        "SUBTENSOR_RPC_URL": "https://rpc.test",
        "GATEWAY_TLS_CERT_B64": "fixture-cert",
        "GATEWAY_TLS_KEY_B64": "fixture-key",
    }


@pytest.mark.parametrize("max_captures", [None, "12", "32"])
def test_split_images_select_the_same_roles_after_gateway_revival(
    values: dict[str, object], max_captures: str | None,
) -> None:
    if max_captures is not None:
        values["WITNESS_MAX_CAPTURES"] = max_captures
    settings = Settings.model_validate(values)
    worker, gateway = worker_spec(settings), gateway_spec(settings)
    assert worker.image == values["WITNESS_IMAGE"]
    assert gateway.image == values["WITNESS_GATEWAY_IMAGE"]
    assert worker.env["WITNESS_ROLE"] == "worker"
    assert gateway.env["WITNESS_ROLE"] == "gateway"
    assert settings.max_captures == int(max_captures or "32")
    assert worker.env["WITNESS_MAX_CAPTURES"] == (max_captures or "32")
    revived = Settings.model_validate({
        **gateway.env,
        "WITNESS_DEPLOYMENT": settings.deployment,
    })
    assert worker_spec(revived).image == worker.image
    assert gateway_spec(revived).image == gateway.image
    assert worker_spec(revived).env["WITNESS_MAX_CAPTURES"] == worker.env["WITNESS_MAX_CAPTURES"]
    assert revived.max_captures == settings.max_captures


def test_gateway_image_is_explicit_for_split_and_legacy_deployments(values: dict[str, object]) -> None:
    values["WITNESS_GATEWAY_IMAGE"] = values["WITNESS_IMAGE"]
    settings = Settings.model_validate(values)
    assert gateway_spec(settings).image == worker_spec(settings).image
    for missing in ("WITNESS_IMAGE", "WITNESS_GATEWAY_IMAGE"):
        with pytest.raises(ValueError, match=missing):
            Settings.model_validate({key: value for key, value in values.items() if key != missing})


@pytest.mark.parametrize("missing", ["R2_REPLAY_BUCKET", "R2_REPLAY_CDN_URL"])
def test_replay_storage_configuration_is_required(
    values: dict[str, object], monkeypatch: pytest.MonkeyPatch, missing: str,
) -> None:
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValueError, match=missing):
        Settings.model_validate({key: value for key, value in values.items() if key != missing})


def test_replay_bucket_is_forwarded_to_workers_after_gateway_revival(values: dict[str, object]) -> None:
    settings = Settings.model_validate(values)
    revived = Settings.model_validate({**gateway_spec(settings).env, "WITNESS_DEPLOYMENT": settings.deployment})
    assert worker_spec(revived).env["R2_REPLAY_BUCKET"] == "fixture-replays"
    assert worker_spec(revived).env["R2_REPLAY_CDN_URL"] == "https://replays.test"
    assert worker_spec(revived).env["R2_BUCKET"] == values["R2_BUCKET"]
    assert worker_spec(revived).env["WITNESS_IMAGE"] == values["WITNESS_IMAGE"]


def test_typed_settings_build_equivalent_pod_specs(values: dict[str, object]) -> None:
    values.update({"WITNESS_DATA_CENTERS": "EU-RO-1,EU-CZ-1", "WITNESS_MAX_PODS": "20"})
    strings = Settings.model_validate(values)
    typed = Settings.model_validate(
        {
            **values,
            "WITNESS_ROUND_END": 2000000000,
            "WITNESS_MASTER_KEY": SecretStr("ab" * 32),
            "GATEWAY_RUNPOD_API_KEY": SecretStr("fixture-runpod"),
            "R2_ENDPOINT": SecretStr("https://r2.test"),
            "R2_ACCESS_KEY_ID": SecretStr("fixture-id"),
            "R2_SECRET_ACCESS_KEY": SecretStr("fixture-secret"),
            "WITNESS_DATA_CENTERS": ("EU-RO-1", "EU-CZ-1"),
            "WITNESS_MAX_PODS": 20,
            "WITNESS_TEST_HOTKEY": None,
        }
    )
    for build in (worker_spec, gateway_spec):
        expected, actual = build(strings), build(typed)
        assert actual.env == expected.env
        assert actual.fingerprint == expected.fingerprint
    assert "WITNESS_TEST_HOTKEY" not in gateway_spec(typed).env
    assert Settings.model_validate(typed).pod_environment == typed.pod_environment


def test_string_launch_values_remain_literal(values: dict[str, object]) -> None:
    values.update(
        {
            "WITNESS_ROUND_END": " 2000000000 ",
            "WITNESS_MAX_PODS": "0250",
            "WITNESS_IDLE_SECONDS": "01800",
        }
    )
    settings = Settings.model_validate(values)
    for name in ("WITNESS_MAX_PODS", "WITNESS_IDLE_SECONDS"):
        assert gateway_spec(settings).env[name] == values[name]
    assert "WITNESS_CPU_ID" not in gateway_spec(settings).env
    assert worker_spec(settings).env["WITNESS_IDLE_SECONDS"] == "01800"


def test_final_saving_requires_and_forwards_embedding_settings(values: dict[str, object]) -> None:
    from test_rounds import Competition

    async def scenario() -> None:
        competition = Competition()
        await competition.controller.refresh()
        inputs = competition.controller.require()
        with pytest.raises(ValueError, match="DINOV3_REVISION"):
            worker_spec(Settings.model_validate(values), inputs)
        values.update(DINOV3_REVISION="fixture-revision", HF_TOKEN="fixture-token")
        spec = worker_spec(Settings.model_validate(values), inputs)
        assert spec.env["DINOV3_REVISION"] == "fixture-revision"
        assert spec.env["HF_TOKEN"] == "fixture-token"

    asyncio.run(scenario())


@pytest.mark.filterwarnings("ignore:It is recommended to use web.RequestKey instances")
@pytest.mark.parametrize("cached_status,cached_lifecycle", [
    ("STARTING", "active"),
    ("RUNNING", "pending"),
])
def test_gateway_polls_faster_for_starting_and_reconnecting_pods(
    values: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    cached_status: str, cached_lifecycle: str,
) -> None:
    from aiohttp import ClientWebSocketResponse
    from aiohttp.test_utils import TestClient, TestServer
    from pod_gateway import main, runpod, server
    from subnet_common.witness.security import capability
    from subnet_common.witness.selection import Selection

    async def scenario() -> None:
        now = time.monotonic()
        (tmp_path / "verifying.bin").write_bytes(b"fixture verification key")
        settings = GatewaySettings.model_validate({
            **values, "WITNESS_PARAMETERS": tmp_path, "GATEWAY_MIN_REQUEST_INTERVAL": 0,
            "WITNESS_START_ID": "00000000-0000-4000-8000-000000000001",
            "RUNPOD_POD_ID": "fixture-gateway",
        })
        gateway_pod = gateway_spec(settings).body(settings.gateway_scope, settings.start_id, epoch_ns=1)
        gateway_pod.update(id=settings.pod_id, status="RUNNING",
                           name=settings.gateway_scope.name(settings.start_id) + "-active")
        spec = worker_spec(settings)
        scope = Scope(settings.deployment, "test-0", Keypair.create_from_uri("//Bob").ss58_address)
        start = "00000000-0000-4000-8000-000000000002"
        pod_id = "fixture-worker"
        pod_name = scope.name(start)
        pod = spec.body(scope, start, epoch_ns=2)
        pod.update(id=pod_id, status=cached_status, name=f"{pod_name}-{cached_lifecycle}",
                   runtime={"ports": [{"type": "tcp", "ip": "8.8.8.8", "private": spec.port, "public": 44001}]})
        state = MemoryState()
        selection = Selection(state)
        selection.update(runpod.RunPodClient._pod(pod), active=True)
        reads = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal reads
            assert request.method == "GET"
            if request.url.path == "/v2/pods":
                reads += 1
                return httpx.Response(200, json={"pods": [gateway_pod, pod], "pagination": {"hasNextPage": False}})
            assert request.url.path == f"/v2/pods/{pod_id}"
            return httpx.Response(200, json=pod)

        gateways: list[server.Gateway] = []
        application = server.Gateway.application

        def capture(gateway: server.Gateway) -> web.Application:
            gateways.append(gateway)
            return application(gateway)

        monkeypatch.setattr(server.Gateway, "application", capture)
        monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: now, time=time.time))
        monkeypatch.setattr(runpod, "time", SimpleNamespace(monotonic=lambda: now))
        monkeypatch.setattr(runpod.RunPodClient, "http_client", lambda: httpx.Client(
            transport=httpx.MockTransport(handle),
        ))
        monkeypatch.setattr("pod_gateway.main.R2StateStore.connect", Mock(return_value=state))
        monkeypatch.setattr(main, "RoundImages", Mock())
        monkeypatch.setattr(main, "RoundController", Mock(
            return_value=SimpleNamespace(inputs=None, refresh=AsyncMock()),
        ))
        monkeypatch.setattr("pod_gateway.main.MembershipRefresher.refresh", lambda _: None)
        app = await main.application(settings, spec)
        gateway = gateways[0]

        async def advance(seconds: float) -> None:
            nonlocal now
            now += seconds
            for _ in range(2):
                changed = gateway._changed
                gateway._wake.set()
                await asyncio.wait_for(changed.wait(), 1)

        async def report_ready(socket: ClientWebSocketResponse) -> None:
            assert (await socket.receive_json(timeout=1))["type"] == "activate"
            await socket.send_json({"type": "ready", "ready": True,
                                    "proof_key_sha256": gateway.proof_hash, "certificate": "fixture-ca"})
            async with asyncio.timeout(1):
                while not gateway.registry.ready(pod_id):
                    await asyncio.sleep(0.005)

        path = f"/v1/control/{scope.hotkey}/{start}"
        headers = {"Authorization": "Bearer " + capability(settings.secret, scope, start), "x-witness-pod": pod_id}
        async with TestClient(TestServer(app)) as client:
            async with asyncio.timeout(1):
                while not gateway._live():
                    await asyncio.sleep(0.005)
            startup_reads = reads
            pod.update(status="RUNNING", name=f"{pod_name}-active")
            await advance(1)
            assert reads == startup_reads
            assert gateway._pods[pod_id].status == cached_status
            assert gateway._pods[pod_id].lifecycle == cached_lifecycle

            async with client.ws_connect(path, headers=headers) as socket:
                await report_ready(socket)
                assert await gateway._endpoint(scope, gateway._cached(scope)) is None
                await advance(1)
                assert reads > startup_reads
                endpoint = await gateway._endpoint(scope, gateway._cached(scope))
                assert endpoint is not None and endpoint["pod_id"] == pod_id
                assert gateway._inventory_refresh_interval() == 15
                idle_reads = reads
                await advance(12)
                assert reads == idle_reads
                await advance(3)
                assert reads > idle_reads

            async with asyncio.timeout(1):
                while pod_id in gateway.registry.peers:
                    await asyncio.sleep(0.005)
            reconnect_reads = reads
            pod["status"] = "STARTING"
            await advance(1)
            assert reads == reconnect_reads
            await advance(1)
            assert reads > reconnect_reads
            assert gateway._pods[pod_id].status == "STARTING"
            pod["status"] = "RUNNING"
            await advance(1)
            assert gateway._pods[pod_id].status == "STARTING"
            async with client.ws_connect(path, headers=headers) as socket:
                await report_ready(socket)
                await advance(1)
                assert await gateway._endpoint(scope, gateway._cached(scope)) is not None

            async with asyncio.timeout(1):
                while pod_id in gateway.registry.peers:
                    await asyncio.sleep(0.005)
            selection.update(runpod.RunPodClient._pod(pod), active=False)
            pod["name"] = f"{pod_name}-draining"
            assert gateway._pods[pod_id].lifecycle == "active"
            async with client.ws_connect(path, headers=headers) as socket:
                assert (await socket.receive_json(timeout=1))["type"] == "drain"

    asyncio.run(scenario())


def test_gateway_refresh_error_preserves_cached_allocation_admission(
    values: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pod_gateway import main
    from test_rounds import Competition

    async def scenario() -> None:
        competition = Competition()
        await competition.controller.refresh()
        frozen = competition.controller.require()
        attempted = asyncio.Event()
        gateway = Mock()
        gateway.application.return_value = web.Application()
        monkeypatch.setattr(main, "Gateway", Mock(return_value=gateway))
        monkeypatch.setattr(main, "RoundController", Mock(return_value=competition.controller))
        monkeypatch.setattr("pod_gateway.main.R2StateStore.connect", Mock(return_value=MemoryState()))
        monkeypatch.setattr(main, "RoundImages", Mock(return_value=MemoryState()))
        monkeypatch.setattr("pod_gateway.main.RunPodClient.http_client", Mock())
        monkeypatch.setattr("pod_gateway.main.MembershipRefresher.refresh", lambda _: None)

        async def unavailable(ref: str) -> str:
            attempted.set()
            raise TimeoutError("Competition refresh unavailable")

        monkeypatch.setattr(competition.source, "get_ref_sha", unavailable)
        (tmp_path / "verifying.bin").write_bytes(b"fixture verification key")
        settings = GatewaySettings.model_validate({**values, "WITNESS_PARAMETERS": tmp_path,
            "WITNESS_START_ID": "00000000-0000-4000-8000-000000000001", "RUNPOD_POD_ID": "fixture-gateway"})
        app = await main.application(settings, worker_spec(settings))
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await asyncio.wait_for(attempted.wait(), 2)
            assert competition.controller.require() == frozen
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["competition", "test"])
def test_gateway_mode_keeps_signed_identity_admission_and_replay_protection(
    values: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from pod_gateway import main
    from test_rounds import Competition

    async def scenario() -> None:
        tester, registered = Keypair.create_from_uri("//Alice"), Keypair.create_from_uri("//Bob")
        competition = Competition()
        membership = Membership(17, "unconfigured")
        now = int(time.time() * 1000)
        membership.snapshot = {
            "version": 1, "network": "finney", "netuid": 17, "block": 1,
            "fetched_at": now, "block_time": now, "hotkeys": [registered.ss58_address],
        }
        gateway = Mock()
        gateway.application.return_value = web.Application()
        gateway.round_changed = AsyncMock()
        gateway_factory = Mock(return_value=gateway)
        refresh = Mock()
        monkeypatch.setattr(main, "Gateway", gateway_factory)
        monkeypatch.setattr(main, "Membership", Mock(return_value=membership))
        monkeypatch.setattr(main, "RoundController", Mock(return_value=competition.controller))
        monkeypatch.setattr("pod_gateway.main.R2StateStore.connect", Mock(return_value=MemoryState()))
        monkeypatch.setattr(main, "RoundImages", Mock(return_value=MemoryState()))
        monkeypatch.setattr("pod_gateway.main.RunPodClient.http_client", Mock())
        monkeypatch.setattr("pod_gateway.main.MembershipRefresher.refresh", refresh)
        (tmp_path / "verifying.bin").write_bytes(b"fixture verification key")
        if mode == "test":
            values.update({
                "WITNESS_MODE": "test", "WITNESS_DEPLOYMENT": "witness-test-v1",
                "WITNESS_COMPETITION_BRANCH": "witness-test", "WITNESS_TEST_HOTKEY": tester.ss58_address,
            })
        settings = GatewaySettings.model_validate({
            **values, "WITNESS_PARAMETERS": tmp_path,
            "WITNESS_START_ID": "00000000-0000-4000-8000-000000000001", "RUNPOD_POD_ID": "fixture-gateway",
        })
        app = await main.application(settings, worker_spec(settings))
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            auth = gateway_factory.call_args.kwargs["auth"]
            allowed, denied = (tester, registered) if mode == "test" else (registered, tester)
            path = "/v1/pods/current"
            headers = hotkey_headers(allowed, b"", method="GET", path=path)
            assert auth.verify("GET", path, b"", headers) == allowed.ss58_address
            with pytest.raises(AuthRejected, match="replayed_hotkey_auth"):
                auth.verify("GET", path, b"", headers)
            forged = hotkey_headers(allowed, b"", method="GET", path=path)
            forged["x-bittensor-signature"] = "0x" + "00" * 64
            with pytest.raises(AuthRejected, match="invalid_signature"):
                auth.verify("GET", path, b"", forged)
            denied_headers = hotkey_headers(denied, b"", method="GET", path=path)
            with pytest.raises(AuthRejected, match="hotkey_not_admitted" if mode == "test" else "hotkey_not_registered"):
                auth.verify("GET", path, b"", denied_headers)
            await asyncio.sleep(0.01)
            if mode == "test":
                refresh.assert_not_called()
            else:
                refresh.assert_called_once()
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("save_submissions", [True, False])
def test_assigned_pod_uses_frozen_inputs_for_final_saving(
    values: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    save_submissions: bool,
) -> None:
    from subnet_common.witness import certs, state
    from witness_pod import direct, pod

    class Started(Exception):
        pass

    saved = MemoryState()
    PricingStore(saved, "competition").publish(PRICING)
    (tmp_path / "verifying.bin").write_bytes(b"fixture verification key")
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))
    for name, value in {
        "WITNESS_ROUND": "test-0", "WITNESS_HOTKEY": str(values["WITNESS_GATEWAY_OWNER_HOTKEY"]),
        "WITNESS_START_ID": "00000000-0000-4000-8000-000000000001", "RUNPOD_POD_ID": "fixture-worker",
        "WITNESS_POD_CAPABILITY": "fixture-token", "WITNESS_RENDERER_ENABLED": "1",
        "WITNESS_ROUND_INPUTS": json.dumps({
            "repository": "404-repo/404-active-competition", "branch": "fixture", "revision": "a" * 40,
            "round": "test-0", "number": 0, "seed": 42, "generation_deadline_block": None,
            "storage_prefix": "", "save_submissions": save_submissions,
            "prompts": [{"id": "official-chair", "sha256": "d" * 64,
                         "source_url": "https://prompts.test/official-chair.png",
                         "image_key": "competition-inputs/v1/" + "d" * 64}],
        }),
        "DINOV3_REVISION": "fixture",
    }.items():
        monkeypatch.setenv(name, value)
    runtime = SimpleNamespace(scope=Scope("competition", "test-0", str(values["WITNESS_GATEWAY_OWNER_HOTKEY"])),
                              drain=lambda: None, spool=tmp_path / "spool")
    service = Mock(side_effect=Started)
    monkeypatch.setattr(pod, "PARAMETERS", tmp_path)
    monkeypatch.setattr(pod, "PodRuntime", Mock(return_value=runtime))
    monkeypatch.setattr("witness_pod.pod.signal.signal", Mock())
    monkeypatch.setattr(pod, "render_service", lambda _: nullcontext())
    monkeypatch.setattr(state, "R2StateStore", Mock(return_value=saved, connect=Mock(return_value=saved)))
    monkeypatch.setattr(certs, "pod_context", Mock(return_value=(None, "fixture-certificate")))
    monkeypatch.setattr(direct, "Witness", service)
    final_objects = Mock(bucket=str(values["R2_BUCKET"]))
    replay_objects = Mock(bucket=str(values["R2_REPLAY_BUCKET"]), keys=Mock(return_value=iter(())))
    monkeypatch.setattr("witness_pod.storage.R2Store", Mock(side_effect=[final_objects, replay_objects]))
    with pytest.raises(Started):
        pod.main()
    store = service.call_args.kwargs["artifacts"]
    if save_submissions:
        assert store.prefix("d" * 64).endswith("/official-chair")
        assert store.objects is final_objects
    else:
        assert store is None
    assert service.call_args.args[1] is replay_objects.url
    assert service.call_args.kwargs["prompt_url"] is final_objects.url
    assert service.call_args.kwargs["render_cache"].objects is replay_objects


def test_settings_import_with_the_shared_modules_packaged_in_the_gateway(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = root / "witness-server/worker/Dockerfile.pods"
    for line in dockerfile.read_text().splitlines():
        if line.startswith("COPY shared/"):
            parts = line.split()
            target = tmp_path / parts[-1].removeprefix("/app/")
            target.mkdir(parents=True, exist_ok=True)
            for name in parts[1:-1]:
                source = root / name
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copyfile(source, target / source.name)
    result = subprocess.run(
        [sys.executable, "-c", "import pod_gateway.settings"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(tmp_path / "shared"), str(root / "witness-gateway")))},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

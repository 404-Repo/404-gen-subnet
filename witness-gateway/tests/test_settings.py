import asyncio
import hashlib
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

import pytest
from aiohttp import web
from bittensor_wallet import Keypair
from pydantic import SecretStr
from support import MemoryState
from witness_client.auth import hotkey_headers

from pod_gateway.artifacts import ArtifactProfile
from pod_gateway.auth import AuthRejected, Membership
from pod_gateway.dependencies import gateway_spec, worker_spec
from pod_gateway.models import Scope
from pod_gateway.renderer import renderer_profile_sha256, require_artifact_profile
from pod_gateway.settings import GatewaySettings, Settings


@pytest.mark.parametrize("round_number", ["test-0", "test-1", "test-2"])
def test_test_hotkey_can_authenticate_without_registration(round_number: str) -> None:
    hotkey = Keypair.create_from_mnemonic(Keypair.generate_mnemonic()).ss58_address
    membership = Membership("test", 17, round_number, hotkey)
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
        Membership("test", 17, "56", "5" * 48)


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
    assert settings.round_refresh_seconds == 60
    assert settings.round_policy_seconds == 120


@pytest.mark.parametrize("refresh,policy", [(60, 60), (61, 120)])
def test_round_refresh_rejects_intervals_outside_policy_bounds(
    values: dict[str, object], refresh: int, policy: int,
) -> None:
    with pytest.raises(ValueError):
        Settings.model_validate({**values, "WITNESS_ROUND_REFRESH_SECONDS": refresh,
                                 "WITNESS_ROUND_POLICY_SECONDS": policy})


@pytest.fixture
def values(tmp_path: Path) -> dict[str, object]:
    pricing = tmp_path / "pricing.json"
    pricing.write_bytes(b"{}")
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
        "SUBTENSOR_RPC_URL": "https://rpc.test",
        "GATEWAY_TLS_CERT_B64": "fixture-cert",
        "GATEWAY_TLS_KEY_B64": "fixture-key",
        "WITNESS_PRICING_FILE": str(pricing),
    }


def test_split_images_select_the_same_roles_after_gateway_revival(values: dict[str, object]) -> None:
    settings = Settings.model_validate(values)
    worker, gateway = worker_spec(settings), gateway_spec(settings)
    assert worker.image == values["WITNESS_IMAGE"]
    assert gateway.image == values["WITNESS_GATEWAY_IMAGE"]
    assert worker.env["WITNESS_ROLE"] == "worker"
    assert gateway.env["WITNESS_ROLE"] == "gateway"
    revived = Settings.model_validate({
        **gateway.env,
        "WITNESS_DEPLOYMENT": settings.deployment,
        "WITNESS_PRICING_FILE": values["WITNESS_PRICING_FILE"],
    })
    assert worker_spec(revived).image == worker.image
    assert gateway_spec(revived).image == gateway.image


def test_gateway_image_is_explicit_for_split_and_legacy_deployments(values: dict[str, object]) -> None:
    values["WITNESS_GATEWAY_IMAGE"] = values["WITNESS_IMAGE"]
    settings = Settings.model_validate(values)
    assert gateway_spec(settings).image == worker_spec(settings).image
    for missing in ("WITNESS_IMAGE", "WITNESS_GATEWAY_IMAGE"):
        with pytest.raises(ValueError, match=missing):
            Settings.model_validate({key: value for key, value in values.items() if key != missing})


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
            "WITNESS_PRICING_FILE": Path(str(values["WITNESS_PRICING_FILE"])),
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


def test_artifact_configuration_reaches_both_pod_roles_and_pins_the_worker_image(values: dict[str, object]) -> None:
    config = json.dumps(
        {
            "profile": {"image_digest": "sha256:" + "a" * 64, "validation_sha256": "b" * 64},
            "limits": {"max_renders": 16, "max_bytes": 256 * 1024 * 1024},
            "seed": 42,
            "commits": {values["WITNESS_GATEWAY_OWNER_HOTKEY"]: "c" * 40},
            "prompt_ids": {"d" * 64: "official-prompt"},
        }
    )
    values["WITNESS_ARTIFACTS"] = config
    values["DINOV3_REVISION"] = "fixture-revision"
    settings = Settings.model_validate(values)
    assert gateway_spec(settings).env["WITNESS_ARTIFACTS"] == config
    assert worker_spec(settings).env["WITNESS_ARTIFACTS"] == config
    values["WITNESS_IMAGE"] = "registry/witness@sha256:" + "f" * 64
    with pytest.raises(ValueError, match="pinned worker image"):
        Settings.model_validate(values)


@pytest.mark.parametrize("commits", [None, {}, {"5" * 48: "c" * 40}])
def test_artifact_commits_do_not_restrict_gateway_membership(
    values: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commits: dict[str, str] | None,
) -> None:
    from pod_gateway import main

    miner = Keypair.create_from_uri("//Alice")
    (tmp_path / "verifying.bin").write_bytes(b"fixture verification key")
    values.update({
        "WITNESS_ARTIFACTS": json.dumps({
            "profile": {"image_digest": "sha256:" + "a" * 64, "validation_sha256": "b" * 64},
            "seed": 42, "prompt_ids": {"d" * 64: "official-chair"},
            **({} if commits is None else {"commits": commits}),
        }),
        "DINOV3_REVISION": "fixture-revision", "WITNESS_PARAMETERS": tmp_path,
        "WITNESS_START_ID": "00000000-0000-4000-8000-000000000001", "RUNPOD_POD_ID": "fixture-gateway",
    })
    gateway = Mock()
    gateway.return_value.application.return_value = web.Application()
    monkeypatch.setattr(main, "Gateway", gateway)
    monkeypatch.setattr("pod_gateway.main.R2StateStore.connect", Mock(return_value=MemoryState()))
    monkeypatch.setattr(main, "RoundImages", Mock(return_value=MemoryState()))
    monkeypatch.setattr("pod_gateway.main.RunPodClient.http_client", Mock())
    monkeypatch.setattr("pod_gateway.main.httpx.Client", Mock())
    settings = GatewaySettings.model_validate(values)
    asyncio.run(main.application(settings, worker_spec(settings)))
    auth = gateway.call_args.kwargs["auth"]
    now = int(time.time() * 1000)
    auth.membership.snapshot = {
        "version": 1, "network": "finney", "netuid": settings.netuid, "block": 1,
        "fetched_at": now, "block_time": now, "hotkeys": [miner.ss58_address],
    }
    path = "/v1/pods/current"
    assert auth.verify("GET", path, b"", hotkey_headers(miner, b"", method="GET", path=path)) == miner.ss58_address
    with pytest.raises(AuthRejected, match="hotkey_not_registered"):
        auth.membership.require(Keypair.create_from_uri("//Bob").ss58_address)


def test_gateway_refresh_error_preserves_cached_round_permission(
    values: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pod_gateway import main
    from test_rounds import Competition

    async def scenario() -> None:
        competition = Competition()
        await competition.controller.refresh()
        frozen = competition.controller.require()
        published = asyncio.Event()
        gateway = Mock()
        gateway.application.return_value = web.Application()
        gateway.publish_policy = AsyncMock(side_effect=published.set)
        monkeypatch.setattr(main, "Gateway", Mock(return_value=gateway))
        monkeypatch.setattr(main, "RoundController", Mock(return_value=competition.controller))
        monkeypatch.setattr("pod_gateway.main.R2StateStore.connect", Mock(return_value=MemoryState()))
        monkeypatch.setattr(main, "RoundImages", Mock(return_value=MemoryState()))
        monkeypatch.setattr("pod_gateway.main.RunPodClient.http_client", Mock())
        monkeypatch.setattr("pod_gateway.main.MembershipRefresher.refresh", lambda _: None)

        async def unavailable(ref: str) -> str:
            raise TimeoutError("Competition refresh unavailable")

        monkeypatch.setattr(competition.source, "get_ref_sha", unavailable)
        (tmp_path / "verifying.bin").write_bytes(b"fixture verification key")
        settings = GatewaySettings.model_validate({**values, "WITNESS_PARAMETERS": tmp_path,
            "WITNESS_START_ID": "00000000-0000-4000-8000-000000000001", "RUNPOD_POD_ID": "fixture-gateway"})
        app = await main.application(settings, worker_spec(settings))
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await asyncio.wait_for(published.wait(), 2)
            assert competition.controller.require() == frozen
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("commits", [None, {}, {"5" * 48: "c" * 40}])
def test_assigned_pod_starts_artifact_storage_without_a_source_commit(
    values: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commits: dict[str, str] | None,
) -> None:
    import pod
    from pod_gateway import certs, direct, renderer, state

    class Started(Exception):
        pass

    pricing = Path(str(values["WITNESS_PRICING_FILE"]))
    pricing.write_bytes(b'{"models":{}}')
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
            "storage_prefix": "", "save_submissions": True,
            "prompts": [{"id": "official-chair", "sha256": "d" * 64,
                         "source_url": "https://prompts.test/official-chair.png",
                         "image_key": "competition-inputs/v1/" + "d" * 64}],
        }),
        "WITNESS_PRICING_SHA256": hashlib.sha256(pricing.read_bytes()).hexdigest(), "DINOV3_REVISION": "fixture",
        "WITNESS_ARTIFACTS": json.dumps({
            "profile": {"image_digest": "sha256:" + "a" * 64, "validation_sha256": "b" * 64},
            "seed": 42, "prompt_ids": {"d" * 64: "official-chair"},
            **({} if commits is None else {"commits": commits}),
        }),
    }.items():
        monkeypatch.setenv(name, value)
    runtime = SimpleNamespace(scope=Scope("competition", "test-0", str(values["WITNESS_GATEWAY_OWNER_HOTKEY"])),
                              drain=lambda: None, spool=tmp_path / "spool")
    service = Mock(side_effect=Started)
    monkeypatch.setattr(pod, "PARAMETERS", tmp_path)
    monkeypatch.setattr(pod, "PodRuntime", Mock(return_value=runtime))
    monkeypatch.setattr("pod.signal.signal", Mock())
    monkeypatch.setattr(pod, "render_service", lambda _: nullcontext())
    monkeypatch.setattr(state, "R2StateStore", Mock(return_value=MemoryState(), connect=Mock(return_value=MemoryState())))
    monkeypatch.setattr(certs, "pod_context", Mock(return_value=(None, "fixture-certificate")))
    monkeypatch.setattr(renderer, "require_artifact_profile", Mock())
    monkeypatch.setattr(direct, "Witness", service)
    monkeypatch.setattr("storage.R2Store", Mock())
    with pytest.raises(Started):
        pod.main()
    store = service.call_args.kwargs["artifacts"]
    assert store.submission_commit is None
    assert store.prefix("d" * 64).endswith("/official-chair")


def test_worker_refuses_a_profile_for_different_packaged_renderer_bytes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    source = tmp_path / "src/server.js"
    source.write_text("export const limit = 200;")
    (tmp_path / "package-lock.json").write_text('{"version":1}')
    profile = ArtifactProfile(image_digest="sha256:" + "a" * 64, validation_sha256=renderer_profile_sha256(tmp_path))
    require_artifact_profile(profile, tmp_path)
    source.write_text("export const limit = 201;")
    with pytest.raises(ValueError, match="packaged renderer"):
        require_artifact_profile(profile, tmp_path)


def test_settings_import_with_the_shared_modules_packaged_in_the_pod(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = root / "witness-server/worker/Dockerfile.pods"
    for line in dockerfile.read_text().splitlines():
        if line.startswith("COPY shared/"):
            parts = line.split()
            target = tmp_path / parts[-1].removeprefix("/app/")
            target.mkdir(parents=True, exist_ok=True)
            for name in parts[1:-1]:
                shutil.copyfile(root / name, target / Path(name).name)
    result = subprocess.run(
        [sys.executable, "-c", "import pod_gateway.settings"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(tmp_path / "shared"), str(root / "witness-gateway")))},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

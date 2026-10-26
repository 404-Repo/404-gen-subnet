import json
import time
from pathlib import Path

import pytest
from bittensor_wallet import Keypair
from pydantic import SecretStr

from pod_gateway.artifacts import ArtifactProfile
from pod_gateway.auth import AuthRejected, Membership
from pod_gateway.dependencies import gateway_spec, worker_spec
from pod_gateway.models import Scope
from pod_gateway.renderer import renderer_profile_sha256, require_artifact_profile
from pod_gateway.settings import Settings


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


@pytest.fixture
def values(tmp_path: Path) -> dict[str, object]:
    pricing = tmp_path / "pricing.json"
    pricing.write_bytes(b"{}")
    return {
        "WITNESS_DEPLOYMENT": "competition",
        "WITNESS_ACTIVE_ROUND": "test-0",
        "WITNESS_ROUND_END": "2000000000",
        "WITNESS_GATEWAY_OWNER_HOTKEY": Keypair.create_from_mnemonic(Keypair.generate_mnemonic()).ss58_address,
        "WITNESS_GATEWAY_URL": "https://witness.test",
        "WITNESS_MASTER_KEY": "ab" * 32,
        "GATEWAY_RUNPOD_API_KEY": "fixture-runpod",
        "WITNESS_IMAGE": "registry/witness@sha256:" + "a" * 64,
        "R2_ENDPOINT": "https://r2.test",
        "R2_ACCESS_KEY_ID": "fixture-id",
        "R2_SECRET_ACCESS_KEY": "fixture-secret",
        "R2_BUCKET": "fixture",
        "SUBTENSOR_RPC_URL": "https://rpc.test",
        "GATEWAY_TLS_CERT_B64": "fixture-cert",
        "GATEWAY_TLS_KEY_B64": "fixture-key",
        "WITNESS_PRICING_FILE": str(pricing),
    }


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
    for name in ("WITNESS_ROUND_END", "WITNESS_MAX_PODS", "WITNESS_IDLE_SECONDS"):
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
            "prompt_hashes": ["d" * 64],
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

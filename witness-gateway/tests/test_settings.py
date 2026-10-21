import time
from decimal import Decimal
from pathlib import Path

import pytest
from bittensor_wallet import Keypair
from pydantic import SecretStr

from pod_gateway.auth import AuthRejected, Membership
from pod_gateway.dependencies import gateway_spec, worker_spec
from pod_gateway.models import Scope
from pod_gateway.settings import Settings


@pytest.mark.parametrize("round_number", ["test-0", "test-1"])
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
        "WITNESS_PROMPT_HASHES": '["' + "a" * 64 + '"]',
        "GATEWAY_RUNPOD_API_KEY": "fixture-runpod",
        "WITNESS_MAX_FLEET_HOURLY": "40.00",
        "GATEWAY_HOURLY_CEILING": "0.15",
        "WITNESS_POD_HOURLY_CEILING": "0.070",
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
            "WITNESS_MAX_FLEET_HOURLY": Decimal("40.00"),
            "WITNESS_POD_HOURLY_CEILING": Decimal("0.070"),
            "WITNESS_PROMPT_HASHES": frozenset({"a" * 64}),
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
            "WITNESS_PROMPT_HASHES": '[ "' + "a" * 64 + '" ]',
            "WITNESS_MAX_PODS": "0250",
            "WITNESS_IDLE_SECONDS": "01800",
        }
    )
    settings = Settings.model_validate(values)
    for name in ("WITNESS_ROUND_END", "WITNESS_PROMPT_HASHES", "WITNESS_MAX_PODS", "WITNESS_IDLE_SECONDS"):
        assert gateway_spec(settings).env[name] == values[name]
    assert "WITNESS_CPU_ID" not in gateway_spec(settings).env
    assert worker_spec(settings).env["WITNESS_IDLE_SECONDS"] == "01800"

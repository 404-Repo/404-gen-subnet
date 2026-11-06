from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from pod_gateway.budget import Budget
from subnet_common.witness.pricing import PricingStore
from subnet_common.witness.state import R2StateStore
from support import MemoryState
from test_recording import BODY, PRICING, SCOPE, invocation
from witness_client.calls import RequestKey, canonical


def extended_pricing() -> bytes:
    table = json.loads(PRICING)
    table["models"]["gpt-6.1-terra"] = {
        "input_credits_per_token": 1,
        "output_credits_per_token": 5,
        "long_input_credits_per_token": 2,
        "long_output_credits_per_token": 7.5,
    }
    return canonical(table)


def test_publishing_adds_models_without_replacing_existing_rows() -> None:
    prices = PricingStore(MemoryState(), SCOPE.deployment)
    directory = Path(__file__).resolve().parents[2] / "miner-reference/budgets"
    prices.publish((directory / "pricing-2026-10-19.json").read_bytes())
    updated = (directory / "pricing.json").read_bytes()
    prices.publish(updated)
    prices.publish(updated)
    assert prices.read() == updated


def test_publish_command_updates_the_shared_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pod_gateway.__main__ import main

    state = MemoryState()
    monkeypatch.setattr(R2StateStore, "connect", Mock(return_value=state))
    for name in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(name, "fixture")
    source = tmp_path / "pricing.json"
    monkeypatch.setattr("sys.argv", ["pod_gateway", "publish-pricing", "--deployment", SCOPE.deployment,
                                     "--pricing", str(source)])
    source.write_bytes(PRICING)
    main()
    source.write_bytes(extended_pricing())
    main()
    assert PricingStore(state, SCOPE.deployment).read() == extended_pricing()


@pytest.mark.parametrize(
    "changes",
    [
        {"models": {}},
        {"models": {"fixture": {"input_credits_per_token": 1, "output_credits_per_token": 1,
                                  "long_input_credits_per_token": 2, "long_output_credits_per_token": 2}}},
        {"calls_per_prompt": 61},
        {"credits_per_prompt": 6_000_000},
        {"long_context_above_input_tokens": 1},
    ],
)
def test_publishing_rejects_changes_to_existing_models_and_budget_rules(changes: dict[str, object]) -> None:
    prices = PricingStore(MemoryState(), SCOPE.deployment)
    prices.publish(PRICING)
    with pytest.raises(ValueError):
        prices.publish(canonical({**json.loads(PRICING), **changes}))
    assert prices.read() == PRICING


def test_running_budget_uses_an_added_model_without_resetting_old_charges() -> None:
    state = MemoryState()
    prices = PricingStore(state, SCOPE.deployment)
    prices.publish(PRICING)
    budget = Budget(state, prices.read(), refresh=prices.read)
    old = invocation()
    budget.reserve(SCOPE, old, "old-capture", BODY)
    prices.publish(extended_pricing())
    body = canonical({"model": "gpt-6.1-terra", "input": "image", "stream": True})
    added = RequestKey.from_body("api", body)
    budget.reserve(SCOPE, added, "new-capture", body)
    usage = {"input_tokens": 3, "output_tokens": 7}
    budget.settle(SCOPE, old, usage)
    budget.settle(SCOPE, added, usage)
    restarted = Budget(state, prices.read(), refresh=prices.read)
    assert Decimal(str(restarted.summary(SCOPE, old)["known_credits"])) == Decimal("3.8")
    assert Decimal(str(restarted.summary(SCOPE, added)["known_credits"])) == Decimal(38)
    assert restarted.summary(SCOPE, old)["possible_dispatches"] == 1

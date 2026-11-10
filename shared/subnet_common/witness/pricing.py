"""Shared model rates and verified usage costs; publishing may only add models."""

import json
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from .state import StateStore


class ModelRates(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    input_credits_per_token: Decimal = Field(ge=0, allow_inf_nan=False)
    output_credits_per_token: Decimal = Field(ge=0, allow_inf_nan=False)
    long_input_credits_per_token: Decimal = Field(ge=0, allow_inf_nan=False)
    long_output_credits_per_token: Decimal = Field(ge=0, allow_inf_nan=False)


class ModelTable(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    calls_per_prompt: int = Field(ge=0, strict=True)
    credits_per_prompt: Decimal = Field(ge=0, allow_inf_nan=False)
    long_context_above_input_tokens: int = Field(ge=0, strict=True)
    models: dict[str, ModelRates]

    def require_extension(self, previous: "ModelTable") -> None:
        if self.model_dump(exclude={"models", "checked_on"}) != previous.model_dump(exclude={"models", "checked_on"}):
            raise ValueError("Model table limits and configuration cannot change")
        if any(self.models.get(model) != rates for model, rates in previous.models.items()):
            raise ValueError("Existing models and rates cannot change or be removed")


class BudgetRejected(ValueError):
    pass


def credits(rates: dict[str, object], model: str, usage: object) -> Decimal | None:
    if usage is None:
        return None
    if not isinstance(usage, dict) or any(
        type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")
    ):
        raise BudgetRejected("Invalid verified token usage")
    if "total_tokens" in usage and usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
        raise BudgetRejected("Inconsistent verified token usage")
    models = rates["models"]
    if not isinstance(models, dict) or model not in models:
        raise BudgetRejected("Unknown priced model")
    rate = models[model]
    prefix = "long_" if usage["input_tokens"] > int(str(rates["long_context_above_input_tokens"])) else ""
    return sum(
        (
            Decimal(str(rate[f"{prefix}{kind}_credits_per_token"])) * usage[f"{kind}_tokens"]
            for kind in ("input", "output")
        ),
        Decimal(0),
    )


class PromptBudget:
    def __init__(self, pricing: bytes) -> None:
        self.rates = json.loads(pricing, parse_float=Decimal)
        self.charges: dict[str, Decimal | None] = {}

    def consume(self, call_id: str, model: str, usage: object, *, not_dispatched: bool = False) -> None:
        if call_id in self.charges or not_dispatched:
            return
        self.charges[call_id] = credits(self.rates, model, usage)

    def accept(self) -> None:
        if any(value is None for value in self.charges.values()):
            raise BudgetRejected("Consumed call has unknown usage")
        total = sum((value for value in self.charges.values() if value is not None), Decimal(0))
        if len(self.charges) > int(self.rates["calls_per_prompt"]) or total > Decimal(
            str(self.rates["credits_per_prompt"])
        ):
            raise BudgetRejected("Replayed prompt exceeds its frozen budget")


class PricingStore:
    def __init__(self, store: StateStore, deployment: str) -> None:
        self.store = store
        self.key = f"pod-pricing/v1/{deployment}.json"

    def read(self) -> bytes:
        snapshot = self.store.load(self.key)
        if snapshot is None:
            raise ValueError("Publish the shared model table before starting workers")
        return snapshot.data

    def publish(self, pricing: bytes) -> None:
        table = ModelTable.model_validate_json(pricing)
        snapshot = self.store.load(self.key)
        if snapshot is not None:
            previous = ModelTable.model_validate_json(snapshot.data)
            table.require_extension(previous)
            if table == previous:
                return
        self.store.save(self.key, pricing, None if snapshot is None else snapshot.etag)

"""One shared model table; publishing may only add models."""

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

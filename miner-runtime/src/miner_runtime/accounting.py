"""Frozen per-prompt accounting over the calls actually consumed by replay."""

from __future__ import annotations

import json
from decimal import Decimal


class BudgetRejected(ValueError):
    pass


def credits(rates: dict[str, object], model: str, usage: object) -> Decimal | None:
    if usage is None:
        return None
    if not isinstance(usage, dict) or any(
        type(usage.get(k)) is not int or usage[k] < 0
        for k in ("input_tokens", "output_tokens")
    ):
        raise BudgetRejected("Invalid verified token usage")
    if (
        "total_tokens" in usage
        and usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]
    ):
        raise BudgetRejected("Inconsistent verified token usage")
    models = rates["models"]
    if not isinstance(models, dict) or model not in models:
        raise BudgetRejected("Unknown priced model")
    rate = models[model]
    prefix = (
        "long_"
        if usage["input_tokens"] > int(str(rates["long_context_above_input_tokens"]))
        else ""
    )
    return sum(
        (
            Decimal(str(rate[f"{prefix}{kind}_credits_per_token"]))
            * usage[f"{kind}_tokens"]
            for kind in ("input", "output")
        ),
        Decimal(0),
    )


class PromptBudget:
    def __init__(self, pricing: bytes) -> None:
        self.rates = json.loads(pricing, parse_float=Decimal)
        self.charges: dict[str, Decimal | None] = {}

    def consume(
        self, call_id: str, model: str, usage: object, *, not_dispatched: bool = False
    ) -> None:
        if call_id in self.charges or not_dispatched:
            return
        self.charges[call_id] = credits(self.rates, model, usage)

    def accept(self) -> None:
        if any(value is None for value in self.charges.values()):
            raise BudgetRejected("Consumed call has unknown usage")
        total = sum(
            (value for value in self.charges.values() if value is not None), Decimal(0)
        )
        if len(self.charges) > int(self.rates["calls_per_prompt"]) or total > Decimal(
            str(self.rates["credits_per_prompt"])
        ):
            raise BudgetRejected("Replayed prompt exceeds its frozen budget")

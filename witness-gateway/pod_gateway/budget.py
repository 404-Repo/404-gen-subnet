"""Frozen competition weights; one ledger per prompt across sessions and seeds."""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from witness_client.calls import Invocation, request_digest

from .models import Scope
from .state import StateConflict, StateStore


class BudgetRejected(RuntimeError):
    pass


class Charge(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    exchange_id: str
    model: str
    state: Literal["possible", "settled", "unknown"] = "possible"
    credits: str | None = None
    usage: dict[str, int] | None = None


class Ledger(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    revision: str
    scope_key: str
    pricing_sha256: str
    charges: dict[str, Charge] = Field(default_factory=dict)


class Budget:
    def __init__(self, store: StateStore, pricing: bytes) -> None:
        self.store = store
        self.pricing_hash = hashlib.sha256(pricing).hexdigest()
        self.rates = json.loads(pricing, parse_float=Decimal)
        if not isinstance(self.rates, dict) or not isinstance(
            self.rates.get("models"), dict
        ):
            raise ValueError("Invalid frozen pricing file")  # noqa: TRY004 - invalid file data
        self.call_limit = int(self.rates["calls_per_prompt"])
        self.credit_limit = Decimal(str(self.rates["credits_per_prompt"]))

    @staticmethod
    def key(scope: Scope, invocation: Invocation) -> str:
        return (
            f"pod-usage/v1/{scope.deployment}/{scope.round}/{scope.hotkey}/"
            f"{invocation.generation.prompt_sha256}.json"
        )

    def _load(self, scope: Scope, invocation: Invocation) -> tuple[Ledger, str | None]:
        snapshot = self.store.load(self.key(scope, invocation))
        ledger = (
            Ledger.model_validate_json(snapshot.data)
            if snapshot is not None
            else Ledger(
                revision=str(uuid.uuid4()),
                scope_key=scope.key,
                pricing_sha256=self.pricing_hash,
            )
        )
        if ledger.scope_key != scope.key or ledger.pricing_sha256 != self.pricing_hash:
            raise BudgetRejected("Prompt ledger identity or frozen pricing changed")
        return ledger, None if snapshot is None else snapshot.etag

    @staticmethod
    def charge_key(invocation: Invocation) -> str:
        return f"{invocation.generation.id}/{invocation.request.id}"

    def reserve(
        self, scope: Scope, invocation: Invocation, exchange_id: str, body: bytes
    ) -> None:
        if request_digest(body) != invocation.request.request_sha256:
            raise BudgetRejected("Verified request differs from reserved request hash")
        request = json.loads(body)
        model = request.get("model")
        if model not in self.rates["models"] or request.get("service_tier") not in (
            "default",
            "priority",
        ):
            raise BudgetRejected("Unknown model or unsupported service tier")
        base = self.charge_key(invocation)
        for _ in range(16):
            ledger, etag = self._load(scope, invocation)
            original = ledger.charges.get(base)
            key = base if original is None else f"{base}/{exchange_id}"
            if original is not None and original.exchange_id == exchange_id:
                raise BudgetRejected("This call already has a possible dispatch")
            if key in ledger.charges:
                # A prior authorization can have released HTTP. Never issue it again.
                raise BudgetRejected("This call already has a possible dispatch")
            known = sum(
                (
                    Decimal(c.credits)
                    for c in ledger.charges.values()
                    if c.credits is not None
                ),
                Decimal(0),
            )
            if len(ledger.charges) >= self.call_limit or known >= self.credit_limit:
                raise BudgetRejected("Prompt budget reached")
            ledger.charges[key] = Charge(exchange_id=exchange_id, model=model)
            ledger.revision = str(uuid.uuid4())
            try:
                self.store.save(
                    self.key(scope, invocation), ledger.model_dump_json().encode(), etag
                )
                return
            except StateConflict:
                continue
        raise StateConflict("Prompt ledger is busy")

    def settle(
        self, scope: Scope, invocation: Invocation, usage: object, *, exchange_id: str | None = None
    ) -> None:
        normalized: dict[str, int] | None = None
        if usage is not None:
            if not isinstance(usage, dict):
                raise BudgetRejected("Invalid verified token usage")
            names = ("input_tokens", "output_tokens")
            if any(type(usage.get(k)) is not int or usage[k] < 0 for k in names):
                raise BudgetRejected("Invalid verified token counts")
            normalized = {k: usage[k] for k in names}
            if "total_tokens" in usage and usage["total_tokens"] != sum(
                normalized.values()
            ):
                raise BudgetRejected("Inconsistent verified token counts")
        for _ in range(16):
            ledger, etag = self._load(scope, invocation)
            base = self.charge_key(invocation)
            charge = ledger.charges.get(base)
            if exchange_id is None and any(key.startswith(base + "/") for key in ledger.charges):
                raise BudgetRejected("Capture attempt is required for settlement")
            if exchange_id is not None and (charge is None or charge.exchange_id != exchange_id):
                charge = ledger.charges.get(f"{base}/{exchange_id}")
            if charge is None:
                if normalized is not None:
                    raise BudgetRejected("Usage has no reserved dispatch")
                return
            credits: str | None = None
            if normalized is not None:
                rate = self.rates["models"][charge.model]
                long = (
                    normalized["input_tokens"]
                    > self.rates["long_context_above_input_tokens"]
                )
                prefix = "long_" if long else ""
                credits = str(
                    sum(
                        (
                            Decimal(str(rate[f"{prefix}{kind}_credits_per_token"]))
                            * normalized[f"{kind}_tokens"]
                            for kind in ("input", "output")
                        ),
                        Decimal(0),
                    )
                )
            if charge.state != "possible":
                if charge.usage != normalized or charge.credits != credits:
                    raise BudgetRejected("Verified usage changed after settlement")
                return
            charge.state = "unknown" if normalized is None else "settled"
            charge.usage, charge.credits = normalized, credits
            ledger.revision = str(uuid.uuid4())
            try:
                self.store.save(
                    self.key(scope, invocation), ledger.model_dump_json().encode(), etag
                )
                return
            except StateConflict:
                continue
        raise StateConflict("Prompt ledger is busy")

    def summary(self, scope: Scope, invocation: Invocation) -> dict[str, object]:
        ledger, _ = self._load(scope, invocation)
        known = sum(
            (
                Decimal(c.credits)
                for c in ledger.charges.values()
                if c.credits is not None
            ),
            Decimal(0),
        )
        pending = sum(c.state != "settled" for c in ledger.charges.values())
        return {
            "pricing_sha256": self.pricing_hash,
            "possible_dispatches": len(ledger.charges),
            "known_credits": str(known),
            "pending_usage": pending,
            "remaining_credits": None
            if pending
            else str(max(Decimal(0), self.credit_limit - known)),
            "breached": known > self.credit_limit
            or len(ledger.charges) > self.call_limit,
        }

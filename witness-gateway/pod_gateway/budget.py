"""Dispatch fencing and usage settlement; prompt acceptance belongs to replay."""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from witness_client.calls import RequestKey, request_digest

from .models import Scope
from .state import StateConflict, StateStore, WriteRejected


class BudgetRejected(RuntimeError):
    pass


class BudgetNotReserved(BudgetRejected):
    """This reservation made no accepted or uncertain dispatch write."""


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
        if not isinstance(self.rates, dict) or not isinstance(self.rates.get("models"), dict):
            raise ValueError("Invalid frozen pricing file")  # noqa: TRY004 - invalid file data

    @staticmethod
    def key(scope: Scope, invocation: RequestKey) -> str:
        return f"pod-usage/v2/{scope.deployment}/{scope.round}/{scope.hotkey}/{invocation.id}.json"

    def _load(self, scope: Scope, invocation: RequestKey) -> tuple[Ledger, str | None]:
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
            raise BudgetRejected("Call ledger identity or frozen pricing changed")
        return ledger, None if snapshot is None else snapshot.etag

    @staticmethod
    def charge_key(invocation: RequestKey) -> str:
        return invocation.id

    def reserve(
        self, scope: Scope, invocation: RequestKey, exchange_id: str, body: bytes,
    ) -> None:
        if request_digest(body) != invocation.request_sha256:
            raise BudgetNotReserved("Verified request differs from reserved request hash")
        request = json.loads(body)
        model = request.get("model")
        if model not in self.rates["models"] or request.get("service_tier") == "flex":
            raise BudgetNotReserved("Unknown model or unsupported service tier")
        base = self.charge_key(invocation)
        for _ in range(16):
            try:
                ledger, etag = self._load(scope, invocation)
            except Exception as error:
                raise BudgetNotReserved("Call ledger could not be read") from error
            if base in ledger.charges:
                raise BudgetRejected("This call already has a possible dispatch")
            ledger.charges[base] = Charge(exchange_id=exchange_id, model=model)
            ledger.revision = str(uuid.uuid4())
            try:
                self.store.save(self.key(scope, invocation), ledger.model_dump_json().encode(), etag)
                return
            except StateConflict:
                continue
            except WriteRejected as error:
                raise BudgetNotReserved("Call ledger write was rejected") from error
        raise BudgetNotReserved("Call ledger is busy")

    def discard(self, scope: Scope, invocation: RequestKey, exchange_id: str) -> None:
        for _ in range(16):
            ledger, etag = self._load(scope, invocation)
            base = self.charge_key(invocation)
            current = ledger.charges.get(base)
            if current is not None and current.exchange_id != exchange_id:
                return
            ledger.charges = {
                key: charge for key, charge in ledger.charges.items() if charge.exchange_id != exchange_id
            }
            ledger.revision = str(uuid.uuid4())
            try:
                self.store.save(self.key(scope, invocation), ledger.model_dump_json().encode(), etag)
                return
            except StateConflict:
                continue
        raise StateConflict("Call ledger is busy")

    def settle(self, scope: Scope, invocation: RequestKey, usage: object, *, exchange_id: str | None = None) -> None:
        normalized: dict[str, int] | None = None
        if usage is not None:
            if not isinstance(usage, dict):
                raise BudgetRejected("Invalid verified token usage")
            names = ("input_tokens", "output_tokens")
            if any(type(usage.get(k)) is not int or usage[k] < 0 for k in names):
                raise BudgetRejected("Invalid verified token counts")
            normalized = {k: usage[k] for k in names}
            if "total_tokens" in usage and usage["total_tokens"] != sum(normalized.values()):
                raise BudgetRejected("Inconsistent verified token counts")
        for _ in range(16):
            ledger, etag = self._load(scope, invocation)
            base = self.charge_key(invocation)
            charge = ledger.charges.get(base)
            if exchange_id is not None and charge is not None and charge.exchange_id != exchange_id:
                charge = ledger.charges.get(exchange_id)
                if charge is None and normalized is None:
                    return
                if charge is None or charge.exchange_id != exchange_id:
                    raise BudgetRejected("Settlement belongs to a different capture attempt")
            if charge is None:
                if normalized is not None:
                    raise BudgetRejected("Usage has no reserved dispatch")
                return
            credits: str | None = None
            if normalized is not None:
                rate = self.rates["models"][charge.model]
                long = normalized["input_tokens"] > self.rates["long_context_above_input_tokens"]
                prefix = "long_" if long else ""
                credits = str(
                    sum(
                        (
                            Decimal(str(rate[f"{prefix}{kind}_credits_per_token"])) * normalized[f"{kind}_tokens"]
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
                self.store.save(self.key(scope, invocation), ledger.model_dump_json().encode(), etag)
                return
            except StateConflict:
                continue
        raise StateConflict("Call ledger is busy")

    def summary(self, scope: Scope, invocation: RequestKey) -> dict[str, object]:
        ledger, _ = self._load(scope, invocation)
        known = sum(
            (Decimal(c.credits) for c in ledger.charges.values() if c.credits is not None),
            Decimal(0),
        )
        pending = sum(c.state != "settled" for c in ledger.charges.values())
        return {
            "pricing_sha256": self.pricing_hash,
            "possible_dispatches": len(ledger.charges),
            "known_credits": str(known),
            "pending_usage": pending,
        }

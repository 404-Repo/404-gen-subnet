"""Deployment-wide capacity reservations survive gateway and Pod replacement."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

from .models import Scope, json_bytes, object_dict
from .state import StateConflict, StateStore


class CapacityReached(RuntimeError):
    pass


class Admission:
    def __init__(
        self,
        store: StateStore,
        *,
        max_pods: int,
        hourly_ceiling: str,
        per_pod_ceiling: str,
    ) -> None:
        self.store, self.max_pods = store, max_pods
        self.hourly, self.per_pod = Decimal(hourly_ceiling), Decimal(per_pod_ceiling)
        if not 1 <= max_pods <= 1000 or not all(
            v.is_finite() and v > 0 for v in (self.hourly, self.per_pod)
        ):
            raise ValueError("Invalid fleet admission bounds")

    def _change(self, scope: Scope, start_id: str, *, remove: bool) -> None:
        key = f"pod-capacity/v1/{scope.deployment}.json"
        slot = f"{scope.key}/{start_id}"
        for _ in range(16):
            saved = self.store.load(key)
            value = object_dict(json.loads(saved.data)) if saved else {"slots": {}}
            slots = object_dict(value["slots"])
            if remove:
                slots.pop(slot, None)
            elif slot not in slots:
                total = sum((Decimal(str(cost)) for cost in slots.values()), Decimal(0))
                if len(slots) >= self.max_pods or total + self.per_pod > self.hourly:
                    raise CapacityReached("Witness fleet capacity reached")
                slots[slot] = str(self.per_pod)
            value["revision"] = str(uuid.uuid4())
            try:
                self.store.save(key, json_bytes(value), saved.etag if saved else None)
                return
            except StateConflict:
                continue
        raise StateConflict("Fleet admission is busy")

    def reserve(self, scope: Scope, start_id: str) -> None:
        self._change(scope, start_id, remove=False)

    def release(self, scope: Scope, start_id: str) -> None:
        self._change(scope, start_id, remove=True)

"""Admission from RunPod inventory, including pending and duplicate real Pods."""

from __future__ import annotations

from decimal import Decimal

from .models import Pod


class CapacityReached(RuntimeError):
    pass


class Admission:
    def __init__(
        self, *, max_pods: int, hourly_ceiling: str, per_pod_ceiling: str
    ) -> None:
        self.max_pods = max_pods
        self.hourly, self.per_pod = Decimal(hourly_ceiling), Decimal(per_pod_ceiling)
        if not 1 <= max_pods <= 1000 or not all(
            v.is_finite() and v > 0 for v in (self.hourly, self.per_pod)
        ):
            raise ValueError("Invalid fleet admission bounds")

    def check(self, inventory: list[Pod], deployment: str, *, pending: int = 0) -> None:
        pods = [
            p
            for p in inventory
            if p.scope() is not None
            and p.markers["WITNESS_DEPLOYMENT"] == deployment
            and p.markers["WITNESS_ROUND"] != "gateway"
        ]
        total = self.per_pod * pending
        for pod in pods:
            cost = self.per_pod if pod.hourly_cost is None else Decimal(pod.hourly_cost)
            if not cost.is_finite() or cost < 0:
                raise CapacityReached("RunPod returned an invalid hourly cost")
            total += max(cost, self.per_pod)
        if len(pods) + pending >= self.max_pods or total + self.per_pod > self.hourly:
            raise CapacityReached("Witness fleet capacity reached")

    def excess(self, inventory: list[Pod], deployment: str) -> list[Pod]:
        """Drain resources that appeared late beyond current provider headroom."""
        pods = [
            p
            for p in inventory
            if p.scope() is not None
            and p.markers["WITNESS_DEPLOYMENT"] == deployment
            and p.markers["WITNESS_ROUND"] != "gateway"
        ]
        costs: dict[str, Decimal] = {}
        for pod in pods:
            cost = self.per_pod if pod.hourly_cost is None else Decimal(pod.hourly_cost)
            if not cost.is_finite() or cost < 0:
                raise CapacityReached("RunPod returned an invalid hourly cost")
            costs[pod.id] = max(cost, self.per_pod)
        count, total = len(pods), sum(costs.values(), Decimal(0))
        victims: list[Pod] = []
        for pod in sorted(
            pods, key=lambda p: (p.lifecycle == "active", -p.epoch_ns, p.id)
        ):
            if count <= self.max_pods and total <= self.hourly:
                break
            victims.append(pod)
            count, total = count - 1, total - costs[pod.id]
        return victims

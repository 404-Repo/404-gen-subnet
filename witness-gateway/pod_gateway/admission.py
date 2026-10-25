"""Admission from RunPod inventory, including pending and duplicate real Pods."""

from __future__ import annotations

from .models import Pod


class CapacityReached(RuntimeError):
    pass


class Admission:
    def __init__(self, *, max_pods: int) -> None:
        self.max_pods = max_pods
        if not 1 <= max_pods <= 1000:
            raise ValueError("Invalid fleet admission bounds")

    def check(self, inventory: list[Pod], deployment: str, *, pending: int = 0) -> None:
        pods = [
            p
            for p in inventory
            if p.scope() is not None
            and p.markers["WITNESS_DEPLOYMENT"] == deployment
            and p.markers["WITNESS_ROUND"] != "gateway"
        ]
        if len(pods) + pending >= self.max_pods:
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
        count = len(pods)
        victims: list[Pod] = []
        for pod in sorted(
            pods, key=lambda p: (p.lifecycle == "active", -p.epoch_ns, p.id)
        ):
            if count <= self.max_pods:
                break
            victims.append(pod)
            count -= 1
        return victims

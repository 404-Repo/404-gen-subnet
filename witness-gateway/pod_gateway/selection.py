"""Durable allocation fencing shared by the gateway and its operator-owned Pods."""

from __future__ import annotations

import json

from .models import Pod, Scope, json_bytes, object_dict
from .state import StateConflict, StateStore


class Selection:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def key(scope: Scope) -> str:
        return f"pod-selection/v1/{scope.deployment}/{scope.round}/{scope.hotkey}.json"

    def selected(self, scope: Scope, start_id: str, pod_id: str) -> bool:
        saved = self.store.load(self.key(scope))
        if saved is None:
            return False
        value = object_dict(json.loads(saved.data))
        return value.get("active") is True and value.get("start_id") == start_id and value.get("pod_id") == pod_id

    def revoked(self, pod: Pod) -> bool:
        scope = pod.scope()
        if scope is None:
            return True
        saved = self.store.load(self.key(scope))
        if saved is None:
            return False
        value = object_dict(json.loads(saved.data))
        return (value.get("start_id") == pod.markers["WITNESS_START_ID"] and value.get("pod_id") != pod.id) or (
            int(str(value["epoch_ns"])) >= pod.epoch_ns and (
                value.get("active") is not True or value.get("pod_id") != pod.id
            )
        )

    def grant(self, pod: Pod) -> bool:
        scope = pod.scope()
        if scope is None:
            raise ValueError("Invalid allocation identity")
        key = self.key(scope)
        for _ in range(16):
            saved = self.store.load(key)
            if saved is None:
                return False
            value = object_dict(json.loads(saved.data))
            if (value.get("active") is not True or value.get("pod_id") != pod.id or
                    value.get("start_id") != pod.markers["WITNESS_START_ID"]):
                return False
            if value.get("access_granted") is True:
                return True
            try:
                self.store.save(key, json_bytes({**value, "access_granted": True}), saved.etag)
                return True
            except StateConflict:
                continue
        raise StateConflict("Allocation selection is busy")

    def revoke_unserved(self, pod: Pod) -> bool:
        scope = pod.scope()
        if scope is None:
            raise ValueError("Invalid allocation identity")
        key = self.key(scope)
        for _ in range(16):
            saved = self.store.load(key)
            if saved is None:
                return False
            value = object_dict(json.loads(saved.data))
            if (value.get("access_granted") is not False or value.get("pod_id") != pod.id or
                    value.get("start_id") != pod.markers["WITNESS_START_ID"]):
                return False
            if value.get("active") is False:
                return True
            try:
                self.store.save(key, json_bytes({**value, "active": False}), saved.etag)
                return True
            except StateConflict:
                continue
        raise StateConflict("Allocation selection is busy")

    def update(self, pod: Pod, *, active: bool) -> None:
        scope = pod.scope()
        if scope is None:
            raise ValueError("Invalid allocation identity")
        key = self.key(scope)
        for _ in range(16):
            saved = self.store.load(key)
            current = None if saved is None else object_dict(json.loads(saved.data))
            value: dict[str, object] = {
                "start_id": pod.markers["WITNESS_START_ID"],
                "pod_id": pod.id,
                "epoch_ns": pod.epoch_ns,
                "active": active,
            }
            if current is not None and current.get("pod_id") == pod.id:
                if "access_granted" in current:
                    value["access_granted"] = current["access_granted"]
            elif active and pod.lifecycle == "pending":
                value["access_granted"] = False
            if current is not None:
                if current == value:
                    return
                if not active and current.get("pod_id") != pod.id:
                    return
                if active and (
                    int(str(current["epoch_ns"])) > pod.epoch_ns
                    or (current.get("start_id") == value["start_id"] and current.get("pod_id") != pod.id)
                    or (current.get("pod_id") == pod.id and current.get("active") is False)
                ):
                    raise StateConflict("Allocation has been fenced")
            try:
                self.store.save(key, json_bytes(value), None if saved is None else saved.etag)
                return
            except StateConflict:
                continue
        raise StateConflict("Allocation selection is busy")

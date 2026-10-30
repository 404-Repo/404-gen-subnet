"""In-memory R2/provider fixtures; no provider credentials or live requests."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

from pod_gateway.models import MARKERS, Pod, object_dict, string
from pod_gateway.state import Snapshot, StateConflict


class MemoryState:
    def __init__(self) -> None:
        self.objects: dict[str, Snapshot] = {}
        self.lock = threading.RLock()
        self.revision = 0

    def load(self, key: str) -> Snapshot | None:
        with self.lock:
            return self.objects.get(key)

    def save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
        with self.lock:
            current = self.objects.get(key)
            if (current.etag if current else None) != expected:
                raise StateConflict("Concurrent fixture update")
            self.revision += 1
            snapshot = Snapshot(str(self.revision), data)
            self.objects[key] = snapshot
            return snapshot

    def read(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None:
        saved = self.load(key)
        if saved is None:
            return None
        if len(saved.data) > limit:
            raise ValueError("Oversized fixture")
        return saved.data

    def get(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None:
        return self.read(key, limit)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        with self.lock:
            current = self.load(key)
            self.save(key, data, current.etag if current else None)

    def delete(self, key: str) -> None:
        with self.lock:
            self.objects.pop(key, None)


class Provider:
    def __init__(self) -> None:
        self.pods: dict[str, Pod] = {}
        self.creates = 0
        self.on_create: Callable[[Pod, dict[str, object]], None] = lambda *_: None
        self.lock = threading.RLock()

    def list(self, *, fresh: bool = False) -> list[Pod]:
        with self.lock:
            return list(self.pods.values())

    def get(self, pod_id: str) -> Pod | None:
        with self.lock:
            return self.pods.get(pod_id)

    def create(self, body: dict[str, object]) -> Pod:
        with self.lock:
            self.creates += 1
            env = object_dict(body["env"])
            pod = Pod(
                f"pod-{self.creates}",
                string(body["name"]),
                string(body["image"]),
                "RUNNING",
                {key: str(env[key]) for key in MARKERS},
                {8443: ("8.8.8.8", 44000 + self.creates)},
                "0.06",
            )
            self.pods[pod.id] = pod
        self.on_create(pod, body)
        return pod

    def delete(self, pod_id: str) -> None:
        with self.lock:
            self.pods.pop(pod_id, None)

    def rename(self, pod_id: str, name: str) -> Pod:
        with self.lock:
            pod = replace(self.pods[pod_id], name=name)
            self.pods[pod_id] = pod
            return pod

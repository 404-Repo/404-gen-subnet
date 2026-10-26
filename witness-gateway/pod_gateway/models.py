"""RunPod ownership metadata and public allocation replies, never credentials."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, cast

Phase = Literal[
    "queued", "draining", "creating", "allocated", "retiring", "rejected", "lost", "retired"
]
MARKERS = (
    "WITNESS_DEPLOYMENT",
    "WITNESS_ROUND",
    "WITNESS_HOTKEY",
    "WITNESS_START_ID",
    "WITNESS_POD_SPEC",
    "WITNESS_BOOT_MODE",
    "WITNESS_EPOCH_NS",
    "WITNESS_REPLACES",
)


def json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def identifier(value: str) -> str:
    if str(uuid.UUID(value)) != value:
        raise ValueError("Expected a canonical session UUID")
    return value


@dataclass(frozen=True)
class Scope:
    """The caller must authenticate this canonical hotkey and its membership first."""

    deployment: str
    round: str
    hotkey: str

    def __post_init__(self) -> None:
        for value in (self.deployment, self.round):
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value):
                raise ValueError("Invalid deployment or round")
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{48}", self.hotkey):
            raise ValueError("Invalid hotkey encoding")

    @property
    def key(self) -> str:
        return f"pod-gateway/v1/{self.deployment}/{self.round}/{self.hotkey}.json"

    def name(self, start_id: str) -> str:
        hotkey_hash = hashlib.sha256(self.hotkey.encode()).hexdigest()[:16]
        return (
            f"w404-{self.deployment[:12]}-{self.round[:12]}-"
            f"{hotkey_hash}-{uuid.UUID(identifier(start_id)).hex}"
        )

    def markers(
        self,
        start_id: str,
        spec_hash: str,
        epoch_ns: int = 0,
        replaces: str | None = None,
    ) -> dict[str, str]:
        return {
            "WITNESS_DEPLOYMENT": self.deployment,
            "WITNESS_ROUND": self.round,
            "WITNESS_HOTKEY": self.hotkey,
            "WITNESS_START_ID": identifier(start_id),
            "WITNESS_POD_SPEC": spec_hash,
            "WITNESS_BOOT_MODE": "dormant",
            "WITNESS_EPOCH_NS": str(epoch_ns),
            "WITNESS_REPLACES": "" if replaces is None else identifier(replaces),
        }


@dataclass(frozen=True)
class PodSpec:
    image: str
    cpu_id: str = "cpu3c"
    vcpu_count: int = 2
    disk_gb: int = 10
    port: int = 8000
    data_centers: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    capability_secret: bytes | None = field(default=None, repr=False)
    registry: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_./:-]+@sha256:[0-9a-f]{64}", self.image):
            raise ValueError("Witness image must be pinned by SHA256 digest")
        if not re.fullmatch(r"[a-z0-9-]+", self.cpu_id):
            raise ValueError("Invalid CPU flavor")
        if (
            type(self.vcpu_count) is not int
            or self.vcpu_count < 2
            or self.vcpu_count & (self.vcpu_count - 1)
            or type(self.disk_gb) is not int
            or self.disk_gb < 1
            or type(self.port) is not int
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("Invalid CPU Pod shape")
        if any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in self.env.items()
        ):
            raise ValueError("Environment must contain strings")
        if set(self.env) & {*MARKERS, "WITNESS_POD_CAPABILITY"} or any(
            k.startswith("RUNPOD_") for k in self.env
        ):
            raise ValueError("Reserved environment variable")
        if self.capability_secret is not None and len(self.capability_secret) < 32:
            raise ValueError("Pod capability master key is too short")
        if self.registry is not None and (
            not isinstance(self.registry, str) or not self.registry or len(self.registry) > 128
        ):
            raise ValueError("Invalid container registry credential ID")
        # Copy caller-owned containers so the fingerprint cannot change in flight.
        if any(
            not isinstance(site, str) or not re.fullmatch(r"[A-Z0-9-]+", site)
            for site in self.data_centers
        ):
            raise ValueError("Invalid data center ID")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "data_centers", tuple(self.data_centers))

    def _body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "image": self.image,
            "cloud": "SECURE",
            "cpu": {"id": self.cpu_id, "vcpuCount": self.vcpu_count},
            "disk": self.disk_gb,
            "ports": [f"{self.port}/tcp"],
            "env": dict(self.env),
        }
        if self.data_centers:
            body["dataCenterIds"] = list(self.data_centers)
        if self.registry is not None:
            body["registry"] = self.registry
        return body

    @property
    def fingerprint(self) -> str:
        key_hash = (
            hashlib.sha256(self.capability_secret).digest()
            if self.capability_secret is not None
            else b""
        )
        return hashlib.sha256(json_bytes(self._body()) + key_hash).hexdigest()

    def body(
        self,
        scope: Scope,
        start_id: str,
        *,
        epoch_ns: int = 0,
        replaces: str | None = None,
    ) -> dict[str, object]:
        from .security import capability

        private = (
            {}
            if self.capability_secret is None
            else {
                "WITNESS_POD_CAPABILITY": capability(
                    self.capability_secret, scope, start_id
                )
            }
        )
        return {
            **self._body(),
            "name": scope.name(start_id) + "-pending",
            "env": {
                **self.env,
                **scope.markers(start_id, self.fingerprint, epoch_ns, replaces),
                **private,
            },
        }


@dataclass(frozen=True)
class Pod:
    id: str
    name: str
    image: str
    status: str
    markers: Mapping[str, str]
    tcp: Mapping[int, tuple[str, int]] = field(default_factory=dict)
    hourly_cost: str | None = None

    def in_scope(self, scope: Scope) -> bool:
        return all(
            self.markers.get(key) == value
            for key, value in (
                ("WITNESS_DEPLOYMENT", scope.deployment),
                ("WITNESS_ROUND", scope.round),
                ("WITNESS_HOTKEY", scope.hotkey),
            )
        )

    def matches(self, scope: Scope, session: Session) -> bool:
        return (
            self.scope() == scope
            and self.markers["WITNESS_START_ID"] == session.start_id
            and self.image == session.image
            and self.markers["WITNESS_POD_SPEC"] == session.spec_hash
        )

    def scope(self) -> Scope | None:
        try:
            scope = Scope(
                self.markers["WITNESS_DEPLOYMENT"],
                self.markers["WITNESS_ROUND"],
                self.markers["WITNESS_HOTKEY"],
            )
            start = identifier(self.markers["WITNESS_START_ID"])
            if (
                set(self.markers) != set(MARKERS)
                or self.markers["WITNESS_BOOT_MODE"] != "dormant"
                or not re.fullmatch(r"[0-9a-f]{64}", self.markers["WITNESS_POD_SPEC"])
                or not self.markers["WITNESS_EPOCH_NS"].isdigit()
                or self.name
                not in {
                    scope.name(start) + "-" + phase
                    for phase in ("pending", "active", "draining", "drained")
                }
            ):
                return None
            if self.markers["WITNESS_REPLACES"]:
                identifier(self.markers["WITNESS_REPLACES"])
            return scope
        except (ValueError, KeyError):
            return None

    @property
    def lifecycle(self) -> str:
        return self.name.rsplit("-", 1)[-1]

    @property
    def epoch_ns(self) -> int:
        return int(self.markers["WITNESS_EPOCH_NS"])

    def session(self) -> Session:
        if self.scope() is None:
            raise ValueError("Pod has invalid ownership metadata")
        phases: dict[str, Phase] = {
            "pending": "creating",
            "active": "allocated",
            "draining": "retiring",
            "drained": "retired",
        }
        return Session(
            self.markers["WITNESS_START_ID"],
            self.markers["WITNESS_REPLACES"] or None,
            phases[self.lifecycle],
            self.markers["WITNESS_POD_SPEC"],
            self.image,
            self.id,
        )


@dataclass(frozen=True)
class Session:
    start_id: str
    replaces: str | None
    phase: Phase
    spec_hash: str
    image: str
    pod_id: str | None = None
    rejection: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "start_id": self.start_id,
            "replaces": self.replaces,
            "phase": self.phase,
            "spec_hash": self.spec_hash,
            "image": self.image,
            "pod_id": self.pod_id,
            "rejection": self.rejection,
        }


def string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Expected a nonempty string")
    return value


def object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError("Expected a JSON object")
    return cast(dict[str, object], value)

"""Explicit runtime configuration. Importing this module performs no live reads."""

from __future__ import annotations

import base64
import hashlib
import json
import ssl
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from .auth import public_key
from .models import PodSpec, Scope
from .state import R2StateStore


@dataclass(frozen=True)
class Settings:
    env: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        Scope(self.deployment, self.round, self.env["WITNESS_GATEWAY_OWNER_HOTKEY"])
        public_key(self.env["WITNESS_GATEWAY_OWNER_HOTKEY"])
        if len(self.secret) < 32 or self.round_end <= 0 or self.round == "gateway":
            raise ValueError("Invalid master key or round deadline")
        origin = urlsplit(self.env["WITNESS_GATEWAY_URL"])
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
            or origin.username
        ):
            raise ValueError("Gateway origin must be HTTPS")
        if self.worker_budget <= 0 or self.pod_ceiling <= 0:
            raise ValueError("Invalid hourly fleet budget")
        if (
            int(self.env.get("WITNESS_IDLE_SECONDS", "1800")) < 1800
            or not 1 <= int(self.env.get("WITNESS_MAX_CAPTURES", "2")) <= 32
            or not 1 <= int(self.env.get("WITNESS_MAX_PODS", "250")) <= 1000
        ):
            raise ValueError("Invalid worker capacity or idle expiry")
        self.worker_spec()
        self.gateway_spec()
        _ = self.prompts
        self.tls()

    @property
    def deployment(self) -> str:
        return self.env["WITNESS_DEPLOYMENT"]

    @property
    def round(self) -> str:
        return self.env["WITNESS_ACTIVE_ROUND"]

    @property
    def round_end(self) -> int:
        return int(self.env["WITNESS_ROUND_END"])

    @property
    def secret(self) -> bytes:
        return bytes.fromhex(self.env["WITNESS_MASTER_KEY"])

    @property
    def gateway_scope(self) -> Scope:
        return Scope(
            self.deployment, "gateway", self.env["WITNESS_GATEWAY_OWNER_HOTKEY"]
        )

    @property
    def worker_budget(self) -> Decimal:
        total = Decimal(self.env["WITNESS_MAX_FLEET_HOURLY"])
        gateway = Decimal(self.env["GATEWAY_HOURLY_CEILING"])
        if not total.is_finite() or not gateway.is_finite() or gateway <= 0:
            raise ValueError("Invalid hourly budget")
        return total - gateway

    @property
    def pod_ceiling(self) -> Decimal:
        value = Decimal(self.env["WITNESS_POD_HOURLY_CEILING"])
        if not value.is_finite():
            raise ValueError("Invalid Pod hourly ceiling")
        return value

    @property
    def prompts(self) -> frozenset[str]:
        values = json.loads(self.env["WITNESS_PROMPT_HASHES"])
        if (
            not isinstance(values, list)
            or not values
            or len(values) > 4096
            or any(
                not isinstance(v, str)
                or len(v) != 64
                or any(c not in "0123456789abcdef" for c in v)
                for v in values
            )
        ):
            raise ValueError("Expected frozen prompt SHA256 hashes")
        return frozenset(values)

    @property
    def pricing_path(self) -> Path:
        return Path(
            self.env.get(
                "WITNESS_PRICING_FILE",
                str(
                    Path(__file__).resolve().parents[2]
                    / "miner-reference/budgets/pricing.json"
                ),
            )
        )

    def state(self) -> R2StateStore:
        return R2StateStore.connect(
            endpoint=self.env["R2_ENDPOINT"],
            access_key=self.env["R2_ACCESS_KEY_ID"],
            secret_key=self.env["R2_SECRET_ACCESS_KEY"],
            bucket=self.env["R2_BUCKET"],
        )

    def worker_spec(self) -> PodSpec:
        environment = {
            k: self.env[k]
            for k in (
                "R2_ENDPOINT",
                "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY",
                "R2_BUCKET",
                "WITNESS_GATEWAY_URL",
                "WITNESS_ROUND_END",
            )
        }
        environment.update(
            {
                "WITNESS_ROLE": "worker",
                "WITNESS_PRICING_FILE": "/app/config/pricing.json",
                "WITNESS_PRICING_SHA256": hashlib.sha256(
                    self.pricing_path.read_bytes()
                ).hexdigest(),
                "WITNESS_IDLE_SECONDS": self.env.get("WITNESS_IDLE_SECONDS", "1800"),
                "WITNESS_MAX_CAPTURES": self.env.get("WITNESS_MAX_CAPTURES", "2"),
            }
        )
        return PodSpec(
            self.env["WITNESS_IMAGE"],
            cpu_id=self.env.get("WITNESS_CPU_ID", "cpu3c"),
            vcpu_count=int(self.env.get("WITNESS_VCPU", "2")),
            disk_gb=int(self.env.get("WITNESS_DISK_GB", "10")),
            env=environment,
            capability_secret=self.secret,
            data_centers=tuple(
                filter(None, self.env.get("WITNESS_DATA_CENTERS", "").split(","))
            ),
        )

    def gateway_spec(self) -> PodSpec:
        names = (
            "WITNESS_ACTIVE_ROUND",
            "WITNESS_GATEWAY_OWNER_HOTKEY",
            "WITNESS_GATEWAY_URL",
            "WITNESS_MASTER_KEY",
            "WITNESS_ROUND_END",
            "WITNESS_PROMPT_HASHES",
            "WITNESS_IMAGE",
            "GATEWAY_RUNPOD_API_KEY",
            "WITNESS_MAX_FLEET_HOURLY",
            "WITNESS_POD_HOURLY_CEILING",
            "GATEWAY_HOURLY_CEILING",
            "R2_ENDPOINT",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET",
            "SUBTENSOR_RPC_URL",
            "GATEWAY_TLS_CERT_B64",
            "GATEWAY_TLS_KEY_B64",
        )
        environment = {name: self.env[name] for name in names}
        for name in (
            "WITNESS_CPU_ID",
            "WITNESS_VCPU",
            "WITNESS_DISK_GB",
            "WITNESS_DATA_CENTERS",
            "WITNESS_MAX_PODS",
            "WITNESS_MAX_CAPTURES",
            "WITNESS_IDLE_SECONDS",
            "WITNESS_NETUID",
            "WITNESS_TEST_HOTKEY",
            "RUNPOD_MIN_REQUEST_INTERVAL",
        ):
            if name in self.env:
                # Use a gateway-specific name for this rate limiter configuration;
                # provider API credentials are never part of a worker PodSpec.
                environment[
                    "GATEWAY_MIN_REQUEST_INTERVAL"
                    if name == "RUNPOD_MIN_REQUEST_INTERVAL"
                    else name
                ] = self.env[name]
        environment.update(
            {
                "WITNESS_ROLE": "gateway",
                "WITNESS_PRICING_FILE": "/app/config/pricing.json",
                "GATEWAY_PORT": "8443",
            }
        )
        return PodSpec(
            self.env.get("WITNESS_GATEWAY_IMAGE", self.env["WITNESS_IMAGE"]),
            cpu_id=self.env.get("GATEWAY_CPU_ID", "cpu3c"),
            vcpu_count=int(self.env.get("GATEWAY_VCPU", "2")),
            disk_gb=10,
            port=8443,
            env=environment,
        )

    def tls(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        with tempfile.TemporaryDirectory(prefix="witness-tls-") as temporary:
            certificate, key = Path(temporary) / "cert.pem", Path(temporary) / "key.pem"
            certificate.write_bytes(
                base64.b64decode(self.env["GATEWAY_TLS_CERT_B64"], validate=True)
            )
            key.write_bytes(
                base64.b64decode(self.env["GATEWAY_TLS_KEY_B64"], validate=True)
            )
            key.chmod(0o600)
            context.load_cert_chain(certificate, key)
        return context

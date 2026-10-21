"""Typed runtime configuration; importing this module does not read the environment."""

import json
from collections.abc import Mapping
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import (
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .auth import public_key
from .models import Scope


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore", frozen=True, hide_input_in_errors=True)

    deployment: str = Field(..., alias="WITNESS_DEPLOYMENT")
    round: str = Field(..., alias="WITNESS_ACTIVE_ROUND")
    round_end: int = Field(..., alias="WITNESS_ROUND_END", gt=0)
    gateway_owner_hotkey: str = Field(..., alias="WITNESS_GATEWAY_OWNER_HOTKEY")
    gateway_url: str = Field(..., alias="WITNESS_GATEWAY_URL")
    master_key: SecretStr = Field(..., alias="WITNESS_MASTER_KEY")
    prompts: Annotated[frozenset[str], NoDecode] = Field(..., alias="WITNESS_PROMPT_HASHES")

    runpod_api_key: SecretStr = Field(..., alias="GATEWAY_RUNPOD_API_KEY")
    runpod_min_request_interval: float = Field(default=1.1, alias="RUNPOD_MIN_REQUEST_INTERVAL")
    max_fleet_hourly: Decimal = Field(..., alias="WITNESS_MAX_FLEET_HOURLY")
    gateway_hourly_ceiling: Decimal = Field(..., alias="GATEWAY_HOURLY_CEILING", gt=0)
    pod_ceiling: Decimal = Field(..., alias="WITNESS_POD_HOURLY_CEILING", gt=0)
    max_pods: int = Field(default=250, alias="WITNESS_MAX_PODS", ge=1, le=1000)
    max_captures: int = Field(default=2, alias="WITNESS_MAX_CAPTURES", ge=1, le=32)
    idle_seconds: int = Field(default=1800, alias="WITNESS_IDLE_SECONDS", ge=1800)

    image: str = Field(..., alias="WITNESS_IMAGE")
    worker_cpu_id: str = Field(default="cpu3c", alias="WITNESS_CPU_ID")
    worker_vcpu_count: int = Field(default=2, alias="WITNESS_VCPU")
    worker_disk_gb: int = Field(default=10, alias="WITNESS_DISK_GB")
    data_centers: Annotated[tuple[str, ...], NoDecode] = Field(default=(), alias="WITNESS_DATA_CENTERS")
    gateway_image: str | None = Field(default=None, alias="WITNESS_GATEWAY_IMAGE")
    gateway_cpu_id: str = Field(default="cpu3c", alias="GATEWAY_CPU_ID")
    gateway_vcpu_count: int = Field(default=2, alias="GATEWAY_VCPU")

    r2_endpoint: SecretStr = Field(..., alias="R2_ENDPOINT")
    r2_access_key_id: SecretStr = Field(..., alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: SecretStr = Field(..., alias="R2_SECRET_ACCESS_KEY")
    r2_bucket: str = Field(..., alias="R2_BUCKET")
    subtensor_rpc_url: str = Field(..., alias="SUBTENSOR_RPC_URL")
    netuid: int = Field(default=17, alias="WITNESS_NETUID")
    test_hotkey: str | None = Field(default=None, alias="WITNESS_TEST_HOTKEY")

    pricing_path: Path = Field(
        default=Path(__file__).resolve().parents[2] / "miner-reference/budgets/pricing.json",
        alias="WITNESS_PRICING_FILE",
    )
    tls_certificate: SecretStr = Field(..., alias="GATEWAY_TLS_CERT_B64")
    tls_key: SecretStr = Field(..., alias="GATEWAY_TLS_KEY_B64")

    _pod_environment: Mapping[str, str] = PrivateAttr(default_factory=dict)

    @model_validator(mode="wrap")
    @classmethod
    def preserve_pod_environment(cls, data: object, handler: ModelWrapValidatorHandler[Self]) -> Self:
        settings = handler(data)
        # Pod fingerprints include literal launch values and whether optional values
        # were supplied. Keep those bytes for serialization; runtime code uses fields.
        if isinstance(data, Mapping):
            environment: dict[str, str] = {}
            for name, field in cls.model_fields.items():
                alias = field.alias
                if alias is None or alias not in data or data[alias] is None:
                    continue
                value = getattr(settings, name)
                if isinstance(data[alias], str):
                    environment[alias] = data[alias]
                elif isinstance(value, SecretStr):
                    environment[alias] = value.get_secret_value()
                elif name == "prompts":
                    environment[alias] = json.dumps(sorted(value))
                elif name == "data_centers":
                    environment[alias] = ",".join(value)
                else:
                    environment[alias] = str(value)
            settings._pod_environment = MappingProxyType(environment)
        return settings

    @field_validator(
        "round_end",
        "max_pods",
        "max_captures",
        "idle_seconds",
        "worker_vcpu_count",
        "worker_disk_gb",
        "gateway_vcpu_count",
        "netuid",
        mode="before",
    )
    @classmethod
    def parse_integer(cls, value: object) -> object:
        return int(value) if isinstance(value, str) else value

    @field_validator("prompts", mode="before")
    @classmethod
    def parse_prompts(cls, value: object) -> frozenset[str]:
        values = json.loads(value) if isinstance(value, str) else value
        if (
            not isinstance(values, (list, tuple, set, frozenset))
            or not values
            or len(values) > 4096
            or any(
                not isinstance(item, str) or len(item) != 64 or any(c not in "0123456789abcdef" for c in item)
                for item in values
            )
        ):
            raise ValueError("Expected frozen prompt SHA256 hashes")
        return frozenset(values)

    @field_validator("data_centers", mode="before")
    @classmethod
    def parse_data_centers(cls, value: object) -> object:
        return tuple(filter(None, value.split(","))) if isinstance(value, str) else value

    @field_validator("gateway_url")
    @classmethod
    def validate_gateway_url(cls, value: str) -> str:
        origin = urlsplit(value)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
            or origin.username
        ):
            raise ValueError("Gateway origin must be HTTPS")
        return value

    @model_validator(mode="after")
    def validate_round(self) -> Self:
        Scope(self.deployment, self.round, self.gateway_owner_hotkey)
        public_key(self.gateway_owner_hotkey)
        if len(self.secret) < 32 or self.round == "gateway":
            raise ValueError("Invalid master key or round deadline")
        if self.worker_budget <= 0:
            raise ValueError("Invalid hourly fleet budget")
        return self

    @cached_property
    def secret(self) -> bytes:
        return bytes.fromhex(self.master_key.get_secret_value())

    @property
    def gateway_scope(self) -> Scope:
        return Scope(self.deployment, "gateway", self.gateway_owner_hotkey)

    @property
    def worker_budget(self) -> Decimal:
        return self.max_fleet_hourly - self.gateway_hourly_ceiling

    @property
    def pod_environment(self) -> Mapping[str, str]:
        """Launch values, preserving original strings and explicit defaults."""
        return self._pod_environment


class GatewaySettings(Settings):
    start_id: str = Field(..., alias="WITNESS_START_ID")
    pod_id: str = Field(..., alias="RUNPOD_POD_ID")
    port: int = Field(default=8443, alias="GATEWAY_PORT")
    min_request_interval: float = Field(default=1.1, alias="GATEWAY_MIN_REQUEST_INTERVAL")
    parameters: Path = Field(default=Path("/app/witness-server/verifier/params"), alias="WITNESS_PARAMETERS")

    @field_validator("port", mode="before")
    @classmethod
    def parse_port(cls, value: object) -> object:
        return cls.parse_integer(value)


class RevivorSettings(Settings):
    ca_file: Path = Field(..., alias="GATEWAY_TLS_CA_FILE")
    caddy_config: Path = Field(..., alias="CADDY_CONFIG_FILE")
    revivor_lock: Path | None = Field(default=None, alias="WITNESS_REVIVOR_LOCK")

    @property
    def lock_path(self) -> Path:
        return self.revivor_lock if self.revivor_lock is not None else self.caddy_config.parent / "revivor.lock"

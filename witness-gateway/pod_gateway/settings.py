"""Typed runtime configuration; importing this module does not read the environment."""

from collections.abc import Mapping
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

from .artifacts import ArtifactConfig, ArtifactProfile
from .auth import public_key
from .models import Scope
from .rounds import repository


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore", frozen=True, hide_input_in_errors=True)

    deployment: str = Field(..., alias="WITNESS_DEPLOYMENT")
    round: str = Field(default="unconfigured", alias="WITNESS_ACTIVE_ROUND")
    round_end: int | None = Field(default=None, alias="WITNESS_ROUND_END", gt=0)
    competition_repo: str = Field(default="404-Repo/404-active-competition", alias="WITNESS_COMPETITION_REPO")
    competition_branch: str = Field(default="main", alias="WITNESS_COMPETITION_BRANCH", min_length=1)
    github_token: SecretStr = Field(..., alias="WITNESS_GITHUB_TOKEN", min_length=1)
    round_refresh_seconds: int = Field(default=60, alias="WITNESS_ROUND_REFRESH_SECONDS", ge=1, le=60)
    round_policy_seconds: int = Field(default=120, alias="WITNESS_ROUND_POLICY_SECONDS", ge=5, le=120)
    gateway_owner_hotkey: str = Field(..., alias="WITNESS_GATEWAY_OWNER_HOTKEY")
    gateway_url: str = Field(..., alias="WITNESS_GATEWAY_URL")
    master_key: SecretStr = Field(..., alias="WITNESS_MASTER_KEY")

    runpod_api_key: SecretStr = Field(..., alias="GATEWAY_RUNPOD_API_KEY")
    runpod_min_request_interval: float = Field(default=1.1, alias="RUNPOD_MIN_REQUEST_INTERVAL")
    max_pods: int = Field(default=250, alias="WITNESS_MAX_PODS", ge=1, le=1000)
    max_captures: int = Field(default=2, alias="WITNESS_MAX_CAPTURES", ge=1, le=32)
    idle_seconds: int = Field(default=1800, alias="WITNESS_IDLE_SECONDS", ge=1800)
    create_interval: int = Field(default=10, alias="GATEWAY_CREATE_INTERVAL_SECONDS", ge=1, le=3600)
    request_interval: int = Field(default=10, alias="GATEWAY_REQUEST_INTERVAL_SECONDS", ge=1, le=300)
    poll_seconds: int = Field(default=30, alias="GATEWAY_POLL_SECONDS", ge=10, le=300)
    max_requests: int = Field(default=64, alias="GATEWAY_MAX_REQUESTS", ge=4, le=256)
    max_queue: int = Field(default=1024, alias="GATEWAY_MAX_QUEUE", ge=1, le=4096)

    image: str = Field(..., alias="WITNESS_IMAGE")
    artifacts: str | None = Field(default=None, alias="WITNESS_ARTIFACTS")
    renderer_profile: str | None = Field(default=None, alias="WITNESS_RENDERER_PROFILE")
    embedding_revision: str | None = Field(default=None, alias="DINOV3_REVISION", min_length=1)
    hf_token: SecretStr | None = Field(default=None, alias="HF_TOKEN")
    registry: str | None = Field(default=None, alias="WITNESS_REGISTRY")
    worker_cpu_id: str = Field(default="cpu3c", alias="WITNESS_CPU_ID")
    worker_vcpu_count: int = Field(default=2, alias="WITNESS_VCPU")
    worker_disk_gb: int = Field(default=10, alias="WITNESS_DISK_GB")
    data_centers: Annotated[tuple[str, ...], NoDecode] = Field(default=(), alias="WITNESS_DATA_CENTERS")
    gateway_image: str = Field(..., alias="WITNESS_GATEWAY_IMAGE", min_length=1)
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
        "create_interval",
        "request_interval",
        "poll_seconds",
        "max_requests",
        "max_queue",
        "worker_vcpu_count",
        "worker_disk_gb",
        "gateway_vcpu_count",
        "netuid",
        "round_refresh_seconds",
        "round_policy_seconds",
        mode="before",
    )
    @classmethod
    def parse_integer(cls, value: object) -> object:
        return int(value) if isinstance(value, str) else value

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
        repository(self.competition_repo)
        if self.test_hotkey is not None:
            raise ValueError("Automatic competition admission requires Finney membership; WITNESS_TEST_HOTKEY is unsupported")
        if self.round_refresh_seconds >= self.round_policy_seconds:
            raise ValueError("Round refresh must be shorter than the admission policy lifetime")
        if self.artifacts is not None or self.renderer_profile is not None:
            if not self.embedding_revision:
                raise ValueError("Artifact saving requires DINOV3_REVISION")
            if self.image.rsplit("@", 1)[-1] != self.profile.image_digest:  # type: ignore[union-attr]
                raise ValueError("Artifact renderer profile must match the pinned worker image")
        return self

    @property
    def profile(self) -> ArtifactProfile | None:
        if self.renderer_profile is not None:
            return ArtifactProfile.model_validate_json(self.renderer_profile)
        return None if self.artifacts is None else ArtifactConfig.model_validate_json(self.artifacts).profile

    @cached_property
    def secret(self) -> bytes:
        return bytes.fromhex(self.master_key.get_secret_value())

    @property
    def gateway_scope(self) -> Scope:
        return Scope(self.deployment, "gateway", self.gateway_owner_hotkey)

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
    caddy_listen: str | None = Field(default=None, alias="CADDY_LISTEN")
    revivor_lock: Path | None = Field(default=None, alias="WITNESS_REVIVOR_LOCK")

    @property
    def lock_path(self) -> Path:
        return self.revivor_lock if self.revivor_lock is not None else self.caddy_config.parent / "revivor.lock"

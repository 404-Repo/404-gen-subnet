from decimal import Decimal
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from miner_runtime.pod import IMAGE_PATTERN
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    github_token: SecretStr
    discord_webhook_url: SecretStr | None = None
    github_repo: str
    github_branch: str = "main"
    generation_image: str = Field(pattern=IMAGE_PATTERN)
    runpod_api_key: SecretStr
    max_hourly_cost: Decimal = Field(default=Decimal("0.25"), gt=0, allow_inf_nan=False)
    cdn_url: str
    credentials_url: str
    credentials_port: int = Field(default=8080, ge=1, le=65535)
    witness_url: str = "https://witness.subnet404.xyz"
    subtensor_rpc_url: str = "https://entrypoint-finney.opentensor.ai"
    state_directory: Path = Path("/var/lib/404-leader-generation")
    codex_home: Path = Field(alias="LEADER_CODEX_HOME")
    codex: str = "codex"
    wallet_path: Path
    wallet_name: str = Field(default="leader", pattern=r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")
    wallet_hotkey: str = Field(default="default", pattern=r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")
    cpu_id: str = "cpu3c"
    vcpus: int = Field(default=8, ge=2, le=32)
    disk_gb: int = Field(default=20, ge=10)
    data_center: str | None = None
    workers: int = Field(default=24, ge=1, le=32)
    parallel_calls: int = Field(default=32, ge=1, le=32)
    parallel_preparations: int = Field(default=4, ge=1, le=32)
    prover_contexts: int = Field(default=4, ge=1, le=8)
    go_cpus: int = Field(default=8, ge=1)
    go_memory_mib: int = Field(default=2048, ge=768)
    progress_interval_seconds: int = Field(default=1200, ge=1200)
    stall_replacement_seconds: int = Field(default=3600, gt=1200)
    credential_check_interval_seconds: int = Field(default=30, ge=1, le=60)
    log_level: str = "INFO"

    @field_validator("cdn_url", "witness_url", "subtensor_rpc_url", "credentials_url")
    @classmethod
    def https_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or url.query:
            raise ValueError("Expected an HTTPS URL without credentials, query or fragment")
        return value.rstrip("/")

    @field_validator("state_directory", "codex_home", "wallet_path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.vcpus & (self.vcpus - 1) or self.go_cpus > self.vcpus:
            raise ValueError("Use power-of-two vCPUs and no more Go CPUs than vCPUs")
        if not self.prover_contexts <= self.parallel_preparations <= self.parallel_calls:
            raise ValueError("Proof contexts must fit preparation slots and active calls")
        if urlsplit(self.witness_url).path:
            raise ValueError("Witness URL must be an HTTPS origin")
        if urlsplit(self.credentials_url).path:
            raise ValueError("Credential URL must be an HTTPS origin")
        return self

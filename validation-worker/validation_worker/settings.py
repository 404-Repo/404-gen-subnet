from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    github_token: SecretStr = Field(..., alias="GITHUB_TOKEN", description="GitHub personal access token")
    github_repo: str = Field(..., alias="GITHUB_REPO", description="Git repo with the current competition state")
    github_branch: str = Field(default="main", alias="GITHUB_BRANCH", description="Competition Git branch to read")

    min_check_state_interval_seconds: int = Field(
        default=120,
        alias="MIN_CHECK_STATE_INTERVAL",
        description="Minimum interval between competition state checks in seconds",
    )
    max_check_state_interval_seconds: int = Field(
        default=1800,
        alias="MAX_CHECK_STATE_INTERVAL",
        description="Maximum interval between competition state checks in seconds",
    )

    log_level: str = Field(default="DEBUG", alias="LOG_LEVEL", description="Logging level")

    witness_competition_branch: str | None = Field(default=None, alias="WITNESS_COMPETITION_BRANCH")
    witness_deployment: str = Field(..., alias="WITNESS_DEPLOYMENT", description="Witness recording deployment")
    r2_endpoint: str = Field(..., alias="R2_ENDPOINT", description="R2 S3 API endpoint")
    r2_bucket: str = Field(..., alias="R2_BUCKET", description="R2 bucket containing witness artifacts")
    r2_replay_bucket: str = Field(
        ..., alias="R2_REPLAY_BUCKET", description="R2 bucket containing capture evidence and cached renders"
    )
    r2_access_key_id: SecretStr = Field(..., alias="R2_ACCESS_KEY_ID", description="R2 read access key")
    r2_secret_access_key: SecretStr = Field(..., alias="R2_SECRET_ACCESS_KEY", description="R2 read secret key")
    witness_provider: Literal["api", "codex"] = Field(default="codex", alias="WITNESS_PROVIDER")
    renderer_id: str = Field(..., alias="RENDERER_ID", description="Original renderer identity for v1 saved previews")
    sandbox_root: Path = Field(..., alias="SANDBOX_ROOT", description="Pristine miner runtime template copied per job")
    sandbox_timeout_seconds: float = Field(default=1800, alias="SANDBOX_TIMEOUT", gt=0)

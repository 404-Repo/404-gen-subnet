from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    github_token: SecretStr = Field(..., alias="GITHUB_TOKEN", description="GitHub personal access token")
    github_repo: str = Field(..., alias="GITHUB_REPO", description="Git repo with the current competition state")
    github_branch: str = Field(default="main", alias="GITHUB_BRANCH", description="Competition Git branch to read")

    runpod_api_key: SecretStr = Field(alias="RUNPOD_PROVIDER_API_KEY")
    pod_image: str = Field(alias="VALIDATION_POD_IMAGE", pattern=r"@sha256:[0-9a-f]{64}$")
    pod_registry: str | None = Field(default=None, alias="VALIDATION_POD_REGISTRY_ID")
    pod_prefix: str = Field(alias="VALIDATION_POD_PREFIX", pattern=r"^validation-[a-z0-9-]{1,40}$")
    pod_data_centers: list[str] = Field(default_factory=list, alias="VALIDATION_POD_DATA_CENTERS")
    worker_environment: dict[str, SecretStr] = Field(alias="VALIDATION_WORKER_ENV")

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

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

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    github_token: SecretStr = Field(..., alias="GITHUB_TOKEN", description="GitHub personal access token")
    github_repo: str = Field(
        ...,
        alias="GITHUB_REPO",
        description="Git repo with the current competition state",
    )
    github_branch: str = Field(default="main", alias="GITHUB_BRANCH", description="Git branch to commit to")

    check_state_interval_seconds: int = Field(
        default=1800,
        alias="CHECK_STATE_INTERVAL",
        description="Interval between competition stage checks in seconds",
    )

    openai_base_url: str = Field(
        default="http://localhost:8000", alias="OPENAI_BASE_URL", description="VLLM server with the judge LLM"
    )

    openai_api_key: SecretStr = Field(..., alias="OPENAI_API_KEY", description="VLLM server API key")

    openai_timeout_seconds: int = Field(
        default=30, alias="OPENAI_TIMEOUT", description="VLLM server timeout in seconds"
    )

    max_concurrent_duels: int = Field(
        default=2, alias="MAX_CONCURRENT_DUELS", description="Maximum number of concurrent duels"
    )

    max_generation_time_seconds: int = Field(
        default=35, alias="MAX_GENERATION_TIME", description="Maximum generation time in seconds"
    )

    overtime_tolerance_ratio: float = Field(
        default=0.1,
        alias="OVERTIME_TOLERANCE_RATIO",
        description="Ratio of overtime prompts allowed before penalization (e.g., 0.1 = first 10% are not penalized)",
    )

    pause_on_stage_end: bool = Field(
        default=False, alias="PAUSE_ON_STAGE_END", description="Pause for inspection or intervention on stage end"
    )

    log_level: str = Field(default="DEBUG", alias="LOG_LEVEL", description="Logging level")


settings = Settings()  # type: ignore[call-arg]

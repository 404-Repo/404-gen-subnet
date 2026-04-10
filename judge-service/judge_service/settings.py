from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # tolerate shared-lib env vars (HF_TOKEN etc.) we don't model here
    )

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
        default=120,
        alias="OPENAI_TIMEOUT",
        description=(
            "Per-request timeout to the VLM server (seconds). At ~50 tok/s generation a "
            "1024-token response is ~22s; pad generously for stages that retry on parse failure."
        ),
    )

    max_concurrent_vlm_calls: int = Field(
        default=32,
        alias="MAX_CONCURRENT_VLM_CALLS",
        description=(
            "Cap on simultaneous VLM calls in flight across the whole match. A duel runs "
            "~25 VLM calls; with N duels gathered you can have up to 25*N in flight. This "
            "sem keeps the VLM endpoint loaded but not over-saturated. Watch vLLM's "
            "Running/Waiting and KV-cache stats — bump until KV cache fills meaningfully."
        ),
    )

    pause_on_stage_end: bool = Field(
        default=False, alias="PAUSE_ON_STAGE_END", description="Pause for inspection or intervention on stage end"
    )

    discord_webhook_url: str | None = Field(
        default=None, alias="DISCORD_WEBHOOK_URL", description="Discord webhook URL for status notifications"
    )

    log_level: str = Field(default="DEBUG", alias="LOG_LEVEL", description="Logging level")

    @field_validator("openai_base_url")
    @classmethod
    def normalize_url(cls, v: str) -> str:
        return v.rstrip("/")

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
            "Hard cap on simultaneous VLM calls in flight, summed across all duels currently "
            "running. Acts as a backstop on top of `max_concurrent_duels`: with K duels in "
            "flight and stage-1/stage-4 gathers of 8 calls each, the worst-case is ~8*K calls "
            "in parallel — set this slightly above that so the sem isn't the inner-duel "
            "bottleneck. Watch vLLM's Running/Waiting and KV-cache stats — bump until the "
            "KV cache fills meaningfully."
        ),
    )

    max_concurrent_duels: int = Field(
        default=4,
        alias="MAX_CONCURRENT_DUELS",
        description=(
            "Cap on simultaneously-running duels in a match. Bounding duel concurrency keeps "
            "each duel's reference image and view PNGs hot in vLLM's prefix cache for the "
            "whole pipeline (~25 calls), instead of getting evicted by sibling duels' prefixes. "
            "Lower = better prefix-cache reuse, less cross-duel parallelism. Tune alongside "
            "`max_concurrent_vlm_calls`."
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

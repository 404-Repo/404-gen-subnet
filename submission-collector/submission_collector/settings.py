from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Pod hosts (Runpod etc.) inject their own env vars (RUNPOD_API_KEY etc.) which
        # would otherwise crash the service on startup. Service-owned typos in .env are
        # a tradeoff: most service config has a defined field below.
        extra="ignore",
    )

    github_token: SecretStr = Field(..., alias="GITHUB_TOKEN", description="GitHub personal access token")
    github_repo: str = Field(
        ...,
        alias="GITHUB_REPO",
        description="Git repo with the current competition state",
    )
    github_branch: str = Field(default="main", alias="GITHUB_BRANCH", description="Git branch to commit to")

    network: str = Field(
        default="finney",
        alias="SUBTENSOR_NETWORK",
        description="Bittensor subtensor endpoint",
    )
    netuid: int = Field(default=17, alias="NETUID", description="Bittensor subnet ID")
    required_lock_alpha: float = Field(
        default=100.0,
        alias="REQUIRED_LOCK_ALPHA",
        description="Required locked ρ (conviction) per submitting hotkey; submissions from coldkeys "
        "locking less than required_lock_alpha x their submitting hotkeys are dropped",
    )
    subtensor_timeout_seconds: int = Field(
        default=120,
        alias="SUBTENSOR_TIMEOUT",
        description="Timeout in seconds for individual subtensor RPC calls",
    )

    min_check_state_interval_seconds: int = Field(
        default=120,
        alias="MIN_CHECK_STATE_INTERVAL",
        description="Minimum interval between competition stage checks in seconds",
    )
    max_check_state_interval_seconds: int = Field(
        default=1800,
        alias="MAX_CHECK_STATE_INTERVAL",
        description="Maximum interval between competition stage checks in seconds",
    )

    max_concurrent_downloads: int = Field(
        default=10,
        alias="MAX_CONCURRENT_DOWNLOADS",
        description="Maximum number of concurrent submission downloads",
    )
    download_jitter_seconds: int = Field(
        default=300,
        alias="DOWNLOAD_JITTER_SECONDS",
        description="Max random delay before collecting each output",
    )

    cdn_url: str = Field(
        default="https://subnet404.xyz", alias="CDN_URL", description="CDN base URL for saved Pod artifacts"
    )

    pause_on_stage_end: bool = Field(
        default=False, alias="PAUSE_ON_STAGE_END", description="Pause for inspection or intervention on stage end"
    )

    render_alert_min_failures: int = Field(
        default=20,
        alias="RENDER_ALERT_MIN_FAILURES",
        description="Minimum render failures before alerting",
    )
    render_alert_min_hotkeys: int = Field(
        default=3,
        alias="RENDER_ALERT_MIN_HOTKEYS",
        description="Minimum distinct hotkeys with render failures before alerting",
    )
    render_alert_cooldown_seconds: int = Field(
        default=600,
        alias="RENDER_ALERT_COOLDOWN_SECONDS",
        description="Minimum seconds between render failure alerts",
    )

    discord_webhook_url: str | None = Field(
        default=None, alias="DISCORD_WEBHOOK_URL", description="Discord webhook URL for status notifications"
    )

    log_level: str = Field(default="DEBUG", alias="LOG_LEVEL", description="Logging level")

    @field_validator("cdn_url")
    @classmethod
    def normalize_url(cls, v: str) -> str:
        return v.rstrip("/")

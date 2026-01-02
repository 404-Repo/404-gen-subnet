from pathlib import Path

from platformdirs import user_cache_dir
from pydantic import Field, SecretStr, field_validator
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
    check_build_interval_seconds: int = Field(
        default=30,
        alias="CHECK_BUILD_INTERVAL",
        description="Interval between build checks in seconds",
    )
    check_pod_interval_seconds: int = Field(
        default=30,
        alias="CHECK_POD_INTERVAL",
        description="Interval between pod checks in seconds",
    )

    build_commit_message: str = Field(
        default="submissions for round {current_round}",
        alias="BUILD_COMMIT_MESSAGE",
        description="Commit message template for build commits",
    )

    build_timeout_seconds: int = Field(
        default=10800,
        alias="BUILD_TIMEOUT",
        description="Docker image build timeout in seconds",
    )
    docker_image_format: str = Field(
        default="europe-docker.pkg.dev/gen-456515/competition-0/{hotkey10}:{tag}",
        alias="DOCKER_IMAGE_FORMAT",
        description="Docker image format string",
    )

    targon_api_key: SecretStr = Field(..., alias="TARGON_API_KEY", description="Targon API key")
    targon_resource: str = Field(
        default="h200-small",
        alias="TARGON_RESOURCE",
        description="Targon resource name used for generations",
    )
    generation_port: int = Field(
        default=10006,
        alias="GENERATION_PORT",
        description="Port used for generations",
    )
    targon_startup_timeout_seconds: float = Field(
        default=1800,
        alias="TARGON_STARTUP_TIMEOUT",
        description="Timeout for pod startup in seconds",
    )
    targon_warmup_timeout_seconds: float = Field(
        default=1800,
        alias="TARGON_WARMUP_TIMEOUT",
        description="Timeout for pod warmup in seconds",
    )

    miner_process_attempts: int = Field(
        default=2,
        alias="MINER_PROCESS_ATTEMPTS",
        description="Number of attempts to process a miner (container deploy + all prompts)",
    )

    max_concurrent_miners: int = Field(
        default=4,
        alias="MAX_CONCURRENT_MINERS",
        description="Maximum number of miners being processed concurrently",
    )

    max_concurrent_prompts_per_miner: int = Field(
        default=4,
        alias="MAX_CONCURRENT_PROMPTS_PER_MINER",
        description="Maximum number of prompts processed concurrently per miner",
    )

    prompt_retry_attempts: int = Field(
        default=2,
        alias="PROMPT_RETRY_ATTEMPTS",
        description="Number of generate+render attempts per prompt before giving up",
    )

    generation_http_attempts: int = Field(
        default=2,
        alias="GENERATION_HTTP_ATTEMPTS",
        description="Number of HTTP request attempts for a single generation call",
    )

    generation_http_backoff_base: float = Field(
        default=2.0,
        alias="GENERATION_HTTP_BACKOFF_BASE",
        description="Base delay in seconds for exponential backoff between generation retries",
    )

    generation_http_backoff_max: float = Field(
        default=30.0,
        alias="GENERATION_HTTP_BACKOFF_MAX",
        description="Maximum delay in seconds between generation retries",
    )

    generation_timeout_seconds: int = Field(
        default=35, alias="GENERATION_TIMEOUT", description="Generation timeout in seconds"
    )

    download_timeout_seconds: int = Field(
        default=180, alias="DOWNLOAD_TIMEOUT", description="Download timeout in seconds"
    )

    overtime_tolerance_ratio: float = Field(
        default=0.1,
        alias="OVERTIME_TOLERANCE_RATIO",
        description="Ratio of overtime prompts allowed before penalization (e.g., 0.1 = first 10% are not penalized)",
    )

    max_failed_prompts_budget: int = Field(
        default=32,
        alias="MAX_FAILED_PROMPTS_BUDGET",
        description="Maximum number of failed prompts allowed before stopping retry attempts entirely",
    )

    render_service_url: str = Field(
        default="http://localhost:8000/", alias="RENDER_URL", description="Render service base URL"
    )

    r2_access_key_id: SecretStr = Field(..., alias="R2_ACCESS_KEY_ID", description="R2 access key ID")
    r2_secret_access_key: SecretStr = Field(..., alias="R2_SECRET_ACCESS_KEY", description="R2 secret access key")
    r2_endpoint: SecretStr = Field(..., alias="R2_ENDPOINT", description="R2 endpoint")

    storage_key_template: str = Field(
        default="rounds/{round}/{hotkey}/{filename}",
        alias="STORAGE_PATH_TEMPLATE",
        description="Storage key template",
    )
    cdn_url: str = Field(default="https://subnet404.xyz", alias="CDN_URL", description="R2 public domain url")

    cache_dir: Path = Field(
        default=Path(user_cache_dir("generation-orchestrator")),
        alias="CACHE_DIR",
        description="Cache directory",
    )

    pause_on_stage_end: bool = Field(
        default=False, alias="PAUSE_ON_STAGE_END", description="Pause for inspection or intervention on stage end"
    )

    log_level: str = Field(default="DEBUG", alias="LOG_LEVEL", description="Logging level")

    debug_keep_pods_alive: bool = Field(
        default=False, alias="DEBUG_KEEP_PODS_ALIVE", description="Do not terminate pods when generation is completed"
    )

    @field_validator("cache_dir", mode="before")
    @classmethod
    def parse_cache_dir(cls, v: str | Path) -> Path:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


settings = Settings()  # type: ignore[call-arg]

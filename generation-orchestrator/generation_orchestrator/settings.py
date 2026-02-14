from pathlib import Path
from typing import Self

from platformdirs import user_cache_dir
from pydantic import Field, SecretStr, field_validator, model_validator
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

    min_check_state_interval_seconds: int = Field(
        default=120,
        alias="MIN_CHECK_STATE_INTERVAL",
        description="Minimum interval between competition stage checks in seconds (default 2 min)",
    )
    max_check_state_interval_seconds: int = Field(
        default=1800,
        alias="MAX_CHECK_STATE_INTERVAL",
        description="Maximum interval between competition stage checks in seconds (default 30 min)",
    )

    check_build_interval_seconds: int = Field(
        default=120,
        alias="CHECK_BUILD_INTERVAL",
        description="Interval between build checks in seconds",
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
        default="europe-west3-docker.pkg.dev/gen-456515/active-competition/{hotkey10}:{tag}",
        alias="DOCKER_IMAGE_FORMAT",
        description="Docker image format string",
    )

    check_audit_interval_seconds: int = Field(
        default=120,
        alias="CHECK_AUDIT_INTERVAL",
        description="Interval between audit checks in seconds (default 2 min)",
    )

    hf_token: SecretStr | None = Field(..., alias="HF_TOKEN", description="HF personal access token")

    gpu_providers: str = Field(
        default="targon",
        alias="GPU_PROVIDERS",
        description="Comma-separated list of GPU providers in priority order (e.g., 'targon,verda')",
    )

    targon_api_key: SecretStr | None = Field(default=None, alias="TARGON_API_KEY", description="Targon API key")
    verda_client_id: SecretStr | None = Field(
        default=None, alias="VERDA_CLIENT_ID", description="Verda OAuth client ID"
    )
    verda_client_secret: SecretStr | None = Field(
        default=None, alias="VERDA_CLIENT_SECRET", description="Verda OAuth client secret"
    )
    verda_generation_token: SecretStr | None = Field(
        default=None, alias="VERDA_GENERATION_TOKEN", description="Verda Bearer token for generation requests"
    )

    generation_port: int = Field(
        default=10006,
        alias="GENERATION_PORT",
        description="Port used for generations",
    )
    check_pod_interval_seconds: int = Field(
        default=30,
        alias="CHECK_POD_INTERVAL",
        description="Interval between pod checks in seconds",
    )
    pod_acquisition_retry_interval_seconds: int = Field(
        default=120,
        alias="POD_ACQUISITION_RETRY_INTERVAL",
        description="Delay between pod acquisition attempts in seconds (default 2 min)",
    )
    pod_acquisition_max_attempts: int = Field(
        default=720,
        alias="POD_ACQUISITION_MAX_ATTEMPTS",
        description="Maximum pod acquisition attempts (default 720 = 24h at 2 min intervals)",
    )
    pod_visibility_timeout_seconds: float = Field(
        default=1800,
        alias="POD_VISIBILITY_TIMEOUT",
        description="Timeout for pod to become visible after deployment in seconds",
    )
    pod_warmup_timeout_seconds: float = Field(
        default=3600,
        alias="POD_WARMUP_TIMEOUT",
        description="Timeout for pod to become healthy after visibility in seconds",
    )

    pods_per_miner: int = Field(
        default=4,
        alias="PODS_PER_MINER",
        description="N: target concurrent PODs per miner (N=1 for testing only, N>=2 for production)",
    )
    max_pod_attempts: int = Field(
        default=6,
        alias="MAX_POD_ATTEMPTS",
        description="K: total POD start attempts budget (K >= N)",
    )
    initial_pod_count: int = Field(
        default=2,
        alias="INITIAL_POD_COUNT",
        description="M: how many PODs to start initially before expansion (M <= N)",
    )
    pod_start_delay_seconds: int = Field(
        default=30,
        alias="POD_START_DELAY",
        description="Delay between POD starts",
    )
    max_prompt_attempts: int = Field(
        default=3,
        alias="MAX_PROMPT_ATTEMPTS",
        description="Max attempts per prompt across all PODs (must be <= pods_per_miner)",
    )
    prompt_lock_timeout_seconds: float = Field(
        default=600.0,
        alias="PROMPT_LOCK_TIMEOUT",
        description="Max seconds a prompt stays assigned to a pod before other pods can claim it",
    )
    pod_min_samples: int = Field(
        default=8,
        alias="POD_MIN_SAMPLES",
        description="Minimum prompts processed before checking bad pod criteria",
    )
    pod_failure_threshold: int = Field(
        default=8,
        alias="POD_FAILURE_THRESHOLD",
        description="Mark POD as bad after this many hard failures",
    )
    warmup_half_window: int = Field(
        default=4,
        alias="WARMUP_HALF_WINDOW",
        description="Half-window size for warmup trend detection (compares median of last N vs previous N)",
    )
    generation_median_limit_seconds: float = Field(
        default=90.0,
        alias="GENERATION_MEDIAN_LIMIT",
        description="Quality bar for trimmed median generation time",
    )
    rolling_median_limit_seconds: float = Field(
        default=120.0,
        alias="ROLLING_MEDIAN_LIMIT",
        description="Infrastructure bar for rolling median (marks pod as bad if exceeded)",
    )
    retry_delta_min_samples: int = Field(
        default=3,
        alias="RETRY_DELTA_MIN_SAMPLES",
        description="Minimum retry samples before checking if retries are helping",
    )
    generation_median_trim_count: int = Field(
        default=6,
        alias="GENERATION_MEDIAN_TRIM_COUNT",
        description="Number of low/high samples trimmed before median calculation",
    )
    generation_timeout_seconds: int = Field(
        default=180,
        alias="GENERATION_TIMEOUT",
        description="Hard limit for generation time in seconds (counts as failure if exceeded)",
    )
    acceptable_distance: float = Field(
        default=0.1,
        alias="ACCEPTABLE_DISTANCE",
        description="Maximum acceptable distance between submitted and regenerated previews",
    )
    max_mismatched_prompts: int = Field(
        default=6,
        alias="MAX_MISMATCHED_PROMPTS",
        description="Maximum tolerated mismatched prompts before early stop",
    )
    max_mismatched_critical_prompts: int = Field(
        default=1,
        alias="MAX_MISMATCHED_CRITICAL_PROMPTS",
        description="Maximum tolerated mismatched critical prompts before audit failure",
    )
    download_timeout_seconds: int = Field(
        default=180, alias="DOWNLOAD_TIMEOUT", description="Download timeout in seconds"
    )

    max_concurrent_miners: int = Field(
        default=4,
        alias="MAX_CONCURRENT_MINERS",
        description="Maximum number of miners being processed concurrently",
    )
    max_concurrent_prompts_per_pod: int = Field(
        default=4,
        alias="MAX_CONCURRENT_PROMPTS_PER_POD",
        description="Maximum number of prompts processed concurrently per GPU pod",
    )

    render_service_url: str = Field(
        default="http://localhost:8000", alias="RENDER_URL", description="Render service base URL"
    )
    image_distance_service_url: str = Field(
        default="http://localhost:8001", alias="IMAGE_DISTANCE_SERVICE_URL", description="Image distance service URL"
    )

    r2_access_key_id: SecretStr = Field(..., alias="R2_ACCESS_KEY_ID", description="R2 access key ID")
    r2_secret_access_key: SecretStr = Field(..., alias="R2_SECRET_ACCESS_KEY", description="R2 secret access key")
    r2_endpoint: SecretStr = Field(..., alias="R2_ENDPOINT", description="R2 endpoint")

    storage_key_template: str = Field(
        default="rounds/{round}/{hotkey}/generated/{filename}",
        alias="STORAGE_PATH_TEMPLATE",
        description="Storage key template",
    )
    cdn_url: str = Field(default="https://subnet404.xyz", alias="CDN_URL", description="R2 public domain url")

    cache_dir: Path = Field(
        default=Path(user_cache_dir("generation-orchestrator")),
        alias="CACHE_DIR",
        description="Cache directory",
    )

    log_level: str = Field(default="DEBUG", alias="LOG_LEVEL", description="Logging level")

    debug_keep_pods_alive: bool = Field(
        default=False, alias="DEBUG_KEEP_PODS_ALIVE", description="Do not terminate pods when generation is completed"
    )

    @field_validator("render_service_url", "image_distance_service_url", "cdn_url")
    @classmethod
    def normalize_url(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("cache_dir", mode="before")
    @classmethod
    def parse_cache_dir(cls, v: str | Path) -> Path:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v

    @model_validator(mode="after")
    def validate_pod_config(self) -> Self:
        """Validate multi-POD configuration constraints."""
        if self.max_prompt_attempts > self.pods_per_miner:
            raise ValueError(
                f"max_prompt_attempts ({self.max_prompt_attempts}) must be <= pods_per_miner ({self.pods_per_miner})"
            )
        if self.max_pod_attempts < self.pods_per_miner:
            raise ValueError(
                f"max_pod_attempts ({self.max_pod_attempts}) must be >= pods_per_miner ({self.pods_per_miner})"
            )
        if self.initial_pod_count > self.pods_per_miner:
            raise ValueError(
                f"initial_pod_count ({self.initial_pod_count}) must be <= pods_per_miner ({self.pods_per_miner})"
            )
        return self

    @model_validator(mode="after")
    def validate_gpu_providers(self) -> Self:
        """Ensure credentials are present for each enabled GPU provider."""
        providers = [p.strip().lower() for p in self.gpu_providers.split(",") if p.strip()]
        if not providers:
            raise ValueError("GPU_PROVIDERS must contain at least one provider")

        for provider in providers:
            if provider not in ("targon", "verda"):
                raise ValueError(f"Unknown GPU provider: {provider}. Valid options: targon, verda")

        if "targon" in providers and not self.targon_api_key:
            raise ValueError("TARGON_API_KEY is required when targon is in GPU_PROVIDERS")

        if "verda" in providers and not all(
            [
                self.verda_client_id,
                self.verda_client_secret,
                self.verda_generation_token,
            ]
        ):
            raise ValueError(
                "VERDA_CLIENT_ID, VERDA_CLIENT_SECRET, and VERDA_GENERATION_TOKEN "
                "are required when verda is in GPU_PROVIDERS"
            )

        return self


settings = Settings()  # type: ignore[call-arg]

from pathlib import Path
from typing import Self

from platformdirs import user_cache_dir
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

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
    dinov3_revision: str = Field(
        ...,
        alias="DINOV3_REVISION",
        description="Hugging Face revision (branch/tag/commit) for the DINOv3 embedding model. "
        "Pin to a commit hash in production — drift across revisions changes embedding "
        "outputs and corrupts judge comparisons.",
    )

    gpu_providers: str = Field(
        default="targon",
        alias="GPU_PROVIDERS",
        description="Comma-separated list of GPU providers in priority order (e.g., 'targon,verda')",
    )
    gpu_type: str = Field(
        default="H200",
        alias="GPU_TYPE",
        description="GPU model miners run on (maps to provider-specific SKUs)",
    )
    gpu_count: int = Field(
        default=4,
        alias="GPU_COUNT",
        description="GPUs per pod; api_specification.md promises miners 4×H200",
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
    runpod_api_key: SecretStr | None = Field(
        default=None,
        alias="RUNPOD_PROVIDER_API_KEY",
        description=(
            "Runpod API key for deploying pods. NOT 'RUNPOD_API_KEY' — Runpod injects "
            "a scope-limited RUNPOD_API_KEY into every pod, which would shadow ours."
        ),
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
        default=12600,
        alias="POD_WARMUP_TIMEOUT",
        description=(
            "Timeout for pod to progress from visible to /status=ready (covers /health + model load). "
            "Default 12600s = 3.5h; combined with 30 min visibility, matches the api-spec 4h warmup budget."
        ),
    )

    batch_size: int = Field(
        default=32,
        alias="BATCH_SIZE",
        description="Number of prompts per batch sent to the pod",
    )
    max_replacements: int = Field(
        default=3,
        alias="MAX_REPLACEMENTS",
        description="Maximum pod replacements allowed per miner (crash or miner-requested)",
    )
    max_initial_deploy_attempts: int = Field(
        default=2,
        alias="MAX_INITIAL_DEPLOY_ATTEMPTS",
        description=(
            "Attempts to get the first healthy pod before giving up; "
            "subsequent deploy failures use the replacement budget"
        ),
    )
    status_check_interval_seconds: float = Field(
        default=10.0,
        alias="STATUS_CHECK_INTERVAL",
        description="Seconds between pod status polls while waiting for batch completion",
    )
    max_consecutive_unhealthy: int = Field(
        default=18,
        alias="MAX_CONSECUTIVE_UNHEALTHY",
        description="Consecutive unreachable status checks before treating pod as crashed (~3 min at 10s interval)",
    )
    batch_time_limit_seconds: float | None = Field(
        default=3600.0,
        alias="BATCH_TIME_LIMIT",
        description=(
            "Per-batch wall-clock cap (seconds). Watchdog only: above any plausible real "
            "batch, exists to break out of pods stuck reporting status=generating forever. "
            "Run-level policy lives in TOTAL_GENERATION_TIME_LIMIT (post-hoc rejection)."
        ),
    )
    total_generation_time_limit_seconds: float = Field(
        default=7200.0,
        alias="TOTAL_GENERATION_TIME_LIMIT",
        description="Maximum total generation time across all batches before audit rejection",
    )
    max_mismatched_prompts: int = Field(
        default=6,
        alias="MAX_MISMATCHED_PROMPTS",
        description="Maximum tolerated mismatched prompts before early stop",
    )
    download_timeout_seconds: int = Field(
        default=180, alias="DOWNLOAD_TIMEOUT", description="Download timeout in seconds"
    )

    max_batch_zip_bytes: int = Field(
        default=64 * 1024 * 1024,
        alias="MAX_BATCH_ZIP_BYTES",
        description="Hard cap on raw /results ZIP size (default 64 MB; api-spec theoretical max is ~32 MB)",
    )
    max_batch_files: int = Field(
        default=100,
        alias="MAX_BATCH_FILES",
        description="Hard cap on archive entries (default 100; expected is 33 = 32 .js + _failed.json)",
    )
    max_js_bytes: int = Field(
        default=1 * 1024 * 1024,
        alias="MAX_JS_BYTES",
        description="Hard cap on a single .js module size; matches output spec FILE_SIZE_EXCEEDED (default 1 MB)",
    )
    max_failed_manifest_bytes: int = Field(
        default=16 * 1024,
        alias="MAX_FAILED_MANIFEST_BYTES",
        description="Hard cap on the _failed.json manifest size (default 16 KB)",
    )

    max_concurrent_miners: int = Field(
        default=4,
        alias="MAX_CONCURRENT_MINERS",
        description="Maximum number of miners being processed concurrently",
    )

    render_service_url: str = Field(
        default="http://localhost:8000", alias="RENDER_URL", description="Render service base URL"
    )
    render_api_key: SecretStr | None = Field(
        default=None,
        alias="RENDER_API_KEY",
        description="Bearer token sent to the render service (e.g. Runpod serverless key). None = no auth.",
    )
    render_timeout_seconds: float = Field(
        default=60.0,
        alias="RENDER_TIMEOUT",
        description="Per-request read timeout for the render service (covers serverless cold starts)",
    )
    render_warmup_timeout_seconds: float = Field(
        default=60.0,
        alias="RENDER_WARMUP_TIMEOUT",
        description="How long the fire-and-forget warmup waits for the cold-start render to return",
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

    discord_progress_interval_seconds: int = Field(
        default=900,
        alias="DISCORD_PROGRESS_INTERVAL",
        description="Interval between Discord progress notifications in seconds (default 15 min)",
    )
    discord_webhook_url: str | None = Field(
        default=None, alias="DISCORD_WEBHOOK_URL", description="Discord webhook URL for status notifications"
    )

    log_level: str = Field(default="DEBUG", alias="LOG_LEVEL", description="Logging level")

    @field_validator("render_service_url", "cdn_url")
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
    def validate_gpu_providers(self) -> Self:
        """Ensure credentials are present for each enabled GPU provider."""
        providers = [p.strip().lower() for p in self.gpu_providers.split(",") if p.strip()]
        if not providers:
            raise ValueError("GPU_PROVIDERS must contain at least one provider")

        for provider in providers:
            if provider not in ("targon", "verda", "runpod"):
                raise ValueError(f"Unknown GPU provider: {provider}. Valid options: targon, verda, runpod")

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

        if "runpod" in providers and not self.runpod_api_key:
            raise ValueError("RUNPOD_PROVIDER_API_KEY is required when runpod is in GPU_PROVIDERS")

        return self

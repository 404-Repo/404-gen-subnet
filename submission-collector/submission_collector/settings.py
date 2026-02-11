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

    network: str = Field(
        default="finney",
        alias="SUBTENSOR_NETWORK",
        description="Bittensor subtensor endpoint",
    )
    netuid: int = Field(default=17, alias="NETUID", description="Network ID")

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

    max_concurrent_downloads: int = Field(
        default=10,
        alias="MAX_CONCURRENT_DOWNLOADS",
        description="Maximum number of concurrent submission downloads",
    )
    download_jitter_seconds: int = Field(
        default=300,
        alias="DOWNLOAD_JITTER_SECONDS",
        description="Max random delay before each download to spread load across CDNs (default 5 min)",
    )
    max_glb_size_bytes: int = Field(
        default=200 * 1024 * 1024,
        alias="MAX_GLB_SIZE_BYTES",
        description="Maximum GLB file size in bytes (default 200 MB)",
    )

    r2_access_key_id: SecretStr = Field(..., alias="R2_ACCESS_KEY_ID", description="R2 access key ID")
    r2_secret_access_key: SecretStr = Field(..., alias="R2_SECRET_ACCESS_KEY", description="R2 secret access key")
    r2_endpoint: SecretStr = Field(..., alias="R2_ENDPOINT", description="R2 endpoint")

    render_service_url: str = Field(
        default="http://localhost:8000", alias="RENDER_URL", description="Render service base URL"
    )

    storage_key_template: str = Field(
        default="rounds/{round}/{hotkey}/submitted/{filename}",
        alias="STORAGE_KEY_TEMPLATE",
        description="Storage key template for uploaded files",
    )
    cdn_url: str = Field(
        default="https://subnet404.xyz", alias="CDN_URL", description="CDN base URL for uploaded files"
    )

    pause_on_stage_end: bool = Field(
        default=False, alias="PAUSE_ON_STAGE_END", description="Pause for inspection or intervention on stage end"
    )

    log_level: str = Field(default="DEBUG", alias="LOG_LEVEL", description="Logging level")

    @field_validator("render_service_url", "cdn_url")
    @classmethod
    def normalize_url(cls, v: str) -> str:
        return v.rstrip("/")

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

    network: str = Field(
        default="finney",
        alias="SUBTENSOR_NETWORK",
        description="Bittensor subtensor endpoint",
    )
    netuid: int = Field(default=17, alias="NETUID", description="Network ID")

    check_state_interval_seconds: int = Field(
        default=1800,
        alias="CHECK_STATE_INTERVAL",
        description="Interval between competition stage checks in seconds",
    )
    submission_delay_blocks: int = Field(
        default=150,
        alias="SUBMISSION_DELAY_BLOCKS",
        description="Submission collection delay after latest_reveal_block in blocks (default: 150 blocks = 30 min)",
    )

    pause_on_stage_end: bool = Field(
        default=False, alias="PAUSE_ON_STAGE_END", description="Pause for inspection or intervention on stage end"
    )

    log_level: str = Field(default="DEBUG", alias="LOG_LEVEL", description="Logging level")

    generation_duration: int = Field(
        default=2*60*60,
        alias="GENERATION_DURATION",
        description="Generation stage duration in seconds when we accept miner submissions.",
    )

    r2_access_key_id: SecretStr = Field(..., alias="R2_ACCESS_KEY_ID", description="R2 access key ID")
    r2_secret_access_key: SecretStr = Field(..., alias="R2_SECRET_ACCESS_KEY", description="R2 secret access key")
    r2_endpoint: SecretStr = Field(..., alias="R2_ENDPOINT", description="R2 endpoint")
    r2_bucket: str = Field(default="404-gen", alias="R2_BUCKET", description="R2 bucket name")

    max_concurrent_requests: int = Field(
        default=10,
        alias="MAX_CONCURRENT_REQUESTS",
        description="Maximum number of concurrent requests to the CDN and R2",
    )


settings = Settings()  # type: ignore[call-arg]

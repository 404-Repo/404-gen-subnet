from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    host: str = Field(default="0.0.0.0", alias="HOST", description="Server host")
    port: int = Field(default=8000, alias="PORT", description="Server port")

    device: str = Field(default="auto", alias="DEVICE", description="Device to use: 'cuda', 'cpu', or 'auto'")

    model_id: str = Field(
        default="facebook/dinov3-vits16-pretrain-lvd1689m",
        alias="MODEL_ID",
        description="Hugging Face model ID for the embedding model",
    )

    model_revision: str | None = Field(
        default=None,
        alias="MODEL_REVISION",
        description="Specific model revision/commit hash",
    )

    hf_token: SecretStr | None = Field(
        default=None,
        alias="HF_TOKEN",
        description="Hugging Face API token for gated/private models",
    )

    download_timeout_seconds: int = Field(
        default=30, alias="DOWNLOAD_TIMEOUT", description="Timeout for downloading images"
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL", description="Logging level")


settings = Settings()  # type: ignore[call-arg]

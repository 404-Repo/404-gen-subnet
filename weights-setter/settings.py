from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    subtensor_endpoint: str = Field(default="finney", description="Endpoint of the Subtensor network")
    netuid: int = Field(default=17, description="Netuid of the Subtensor network")
    set_weights_interval_sec: int = Field(default=1800, ge=60, description="Interval in seconds to set weights")
    set_weights_retry_interval_sec: int = Field(
        default=300,
        ge=10,
        description="Interval in seconds to retry setting weights",
    )
    next_leader_wait_interval_sec: int = Field(
        default=1500,
        ge=60,
        description="Interval in seconds to wait for settings weights for next leader. "
        "If next leader is changed soon then set weight just after it becomes active.",
    )
    subnet_owner_uid: int = Field(default=199, ge=0, description="UID of the network owner")

    wallet_name: str = Field(description="Name of the wallet")
    wallet_hotkey: str = Field(description="Hotkey of the wallet")
    wallet_path: str | None = Field(default=None, description="Path to the wallet")

    github_winner_info_repo: str = Field(default="404-Repo/404-competition-0")
    github_winner_info_branch: str = Field(default="main")
    github_token: str | None = Field(default=None)


settings = Settings()  # type: ignore[call-arg]

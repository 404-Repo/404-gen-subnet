import pytest
from pydantic import SecretStr

from validation_service.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        GITHUB_TOKEN=SecretStr("test-token"),
        GITHUB_REPO="test/competition",
        GITHUB_BRANCH="competition",
        MIN_CHECK_STATE_INTERVAL=120,
        MAX_CHECK_STATE_INTERVAL=1800,
        LOG_LEVEL="DEBUG",
    )

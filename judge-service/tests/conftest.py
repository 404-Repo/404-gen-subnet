import pytest
from pydantic import SecretStr

from judge_service.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        GITHUB_TOKEN=SecretStr("test-token"),
        GITHUB_REPO="test/repo",
        OPENAI_API_KEY=SecretStr("test-key"),
        PAUSE_ON_STAGE_END=False,
    )

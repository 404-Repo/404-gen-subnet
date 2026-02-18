import pytest
from pydantic import SecretStr

from generation_orchestrator.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        GITHUB_TOKEN=SecretStr("test-token"),
        GITHUB_REPO="test/repo",
        HF_TOKEN=SecretStr("test-hf-token"),
        TARGON_API_KEY=SecretStr("test-targon-key"),
        R2_ACCESS_KEY_ID=SecretStr("test-r2-key"),
        R2_SECRET_ACCESS_KEY=SecretStr("test-r2-secret"),
        R2_ENDPOINT=SecretStr("https://test.r2.example.com"),
        POD_START_DELAY=0,
    )

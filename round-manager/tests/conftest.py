import pytest
from pydantic import SecretStr
from subnet_common.testing import MockGitHubClient

from round_manager.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(GITHUB_TOKEN=SecretStr("test-token"), GITHUB_REPO="test/repo")


@pytest.fixture
def git() -> MockGitHubClient:
    return MockGitHubClient()


def make_get_block(block: int = 7200):
    """Factory to create a controllable get_block function."""

    async def get_block() -> int:
        return block

    return get_block

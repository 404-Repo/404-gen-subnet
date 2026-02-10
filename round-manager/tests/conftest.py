from dataclasses import dataclass, field

import pytest
from pydantic import SecretStr

from round_manager.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(GITHUB_TOKEN=SecretStr("test-token"), GITHUB_REPO="test/repo")


@dataclass
class MockGitHubClient:
    """Controllable mock for GitHubClient."""

    files: dict[str, str] = field(default_factory=dict)
    ref_sha: str = "abc123"
    _commits: list[dict] = field(default_factory=list)

    async def get_ref_sha(self, ref: str) -> str:
        return self.ref_sha

    async def get_file(self, path: str, ref: str) -> str | None:
        return self.files.get(path)

    async def commit_files(
        self,
        files: dict[str, str],
        message: str,
        branch: str,
        base_sha: str | None = None,
    ) -> str:
        self._commits.append(
            {
                "files": files,
                "message": message,
                "branch": branch,
                "base_sha": base_sha,
            }
        )
        return f"{self.ref_sha}_new"

    @property
    def committed(self) -> dict[str, str]:
        """All committed files flattened (later commits override earlier)."""
        result: dict[str, str] = {}
        for commit in self._commits:
            result.update(commit["files"])
        return result


@pytest.fixture
def git() -> MockGitHubClient:
    return MockGitHubClient()


def make_get_block(block: int = 7200):
    """Factory to create a controllable get_block function."""

    async def get_block() -> int:
        return block

    return get_block

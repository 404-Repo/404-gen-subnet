from dataclasses import dataclass, field

from subnet_common.github import GitHubClient


@dataclass
class MockGitHubClient(GitHubClient):
    """Controllable mock for GitHubClient."""

    files: dict[str, str] = field(default_factory=dict)
    ref_sha: str = "abc123"
    _commits: list[dict] = field(default_factory=list)

    def __init__(
        self,
        files: dict[str, str] | None = None,
        ref_sha: str = "abc123",
    ) -> None:
        # Skip GitHubClient.__init__ — we don't need a real HTTP client
        self.files: dict[str, str] = files or {}
        self.ref_sha = ref_sha
        self._commits: list[dict[str, object]] = []

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

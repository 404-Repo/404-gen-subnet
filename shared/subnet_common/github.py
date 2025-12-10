from types import TracebackType
from typing import Self

import httpx
from loguru import logger
from pydantic import BaseModel


class GitHubJob(BaseModel):
    id: int
    name: str
    status: str  # queued, in_progress, completed
    conclusion: str | None  # success, failure, cancelled, skipped


class GitHubClient:
    """Async GitHub API client."""

    def __init__(
        self,
        repo: str,
        *,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
    ):
        self.repo = repo
        self.base_url = base_url
        self._headers = {
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._owns_client = False

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=self._timeout,
        )
        self._owns_client = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._owns_client and self._client:
            await self._client.aclose()
            self._client = None

    @classmethod
    def with_client(
        cls,
        client: httpx.AsyncClient,
        repo: str,
        token: str | None = None,
    ) -> Self:
        """Use an existing httpx client (caller manages lifecycle)."""
        instance = cls(repo, token=token)
        instance._client = client
        instance._owns_client = False
        return instance

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Client not initialized. Use 'async with' or 'with_client()'.")
        return self._client

    async def get_ref_sha(self, ref: str) -> str:
        response = await self.client.get(f"/repos/{self.repo}/commits/{ref}")
        response.raise_for_status()
        return str(response.json()["sha"])

    async def get_file(self, path: str, ref: str) -> str | None:
        """Read file content at a specific ref (branch/tag/commit)."""
        response = await self.client.get(
            f"/repos/{self.repo}/contents/{path}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    async def commit_files(
        self,
        files: dict[str, str],
        message: str,
        branch: str,
        base_sha: str | None = None,
    ) -> str:
        """Commit multiple files atomically. Returns new commit SHA."""
        if base_sha is None:
            base_sha = await self.get_ref_sha(f"heads/{branch}")

        tree_sha = await self._get_tree_sha(base_sha)

        tree_items = [
            {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": await self._create_blob(content),
            }
            for path, content in files.items()
        ]

        new_tree_sha = await self._create_tree(tree_sha, tree_items)
        new_commit_sha = await self._create_commit(message, new_tree_sha, base_sha)
        await self._update_ref(f"heads/{branch}", new_commit_sha)

        logger.info(f"Committed to {branch}: {new_commit_sha}")
        return new_commit_sha

    async def find_run_by_commit_message(self, message: str) -> int | None:
        """Find a workflow run ID with the commit message that has a specified ending."""
        response = await self.client.get(f"/repos/{self.repo}/actions/runs")
        response.raise_for_status()

        for run in response.json().get("workflow_runs", []):
            if run["head_commit"]["message"].endswith(message):
                logger.debug(f"Found run {run['id']} for message: {message[:50]}")
                return int(run["id"])

        return None

    async def get_jobs(self, run_id: int) -> list[GitHubJob]:
        """Get all jobs for a workflow run."""
        jobs: list[GitHubJob] = []
        page = 1

        while True:
            response = await self.client.get(
                f"/repos/{self.repo}/actions/runs/{run_id}/jobs",
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()

            batch = response.json().get("jobs", [])
            if not batch:
                break

            jobs.extend(GitHubJob(**j) for j in batch)

            if len(batch) < 100:
                break
            page += 1

        logger.debug(f"Fetched {len(jobs)} jobs for run {run_id}")
        return jobs

    async def _get_tree_sha(self, commit_sha: str) -> str:
        response = await self.client.get(f"/repos/{self.repo}/git/commits/{commit_sha}")
        response.raise_for_status()
        return str(response.json()["tree"]["sha"])

    async def _create_blob(self, content: str) -> str:
        response = await self.client.post(
            f"/repos/{self.repo}/git/blobs",
            json={"content": content, "encoding": "utf-8"},
        )
        response.raise_for_status()
        return str(response.json()["sha"])

    async def _create_tree(self, base_sha: str, items: list[dict]) -> str:
        response = await self.client.post(
            f"/repos/{self.repo}/git/trees",
            json={"base_tree": base_sha, "tree": items},
        )
        response.raise_for_status()
        return str(response.json()["sha"])

    async def _create_commit(self, message: str, tree_sha: str, parent_sha: str) -> str:
        response = await self.client.post(
            f"/repos/{self.repo}/git/commits",
            json={"message": message, "tree": tree_sha, "parents": [parent_sha]},
        )
        response.raise_for_status()
        return str(response.json()["sha"])

    async def _update_ref(self, ref: str, sha: str) -> None:
        response = await self.client.patch(
            f"/repos/{self.repo}/git/refs/{ref}",
            json={"sha": sha, "force": False},
        )
        response.raise_for_status()

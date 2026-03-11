from __future__ import annotations

import asyncio
import json

from subnet_common.competition.build_info import BuildStatus
from subnet_common.competition.submissions import MinerSubmission
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubJob
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.testing import MockGitHubClient

from generation_orchestrator.build_tracker import BuildTracker
from generation_orchestrator.generation_stop import GenerationStop


HOTKEY = "5abc123def"
JOB_NAME = f"Bake and push images to Google Cloud ({HOTKEY})"


class BuildMockGitHub(MockGitHubClient):
    """MockGitHubClient extended with build-tracking API stubs."""

    def __init__(self) -> None:
        super().__init__()
        self.run_id: int | None = None
        self.jobs: list[GitHubJob] = []
        self._shutdown: GracefulShutdown | None = None

    async def find_run_by_commit_message(self, message: str) -> int | None:
        await asyncio.sleep(0)  # yield so concurrent wait_for_build can start
        if self.run_id is None and self._shutdown is not None:
            self._shutdown.request_shutdown()
        return self.run_id

    async def get_jobs(self, run_id: int) -> list[GitHubJob]:
        await asyncio.sleep(0)
        return self.jobs


def make_submission(hotkey: str = HOTKEY) -> dict[str, MinerSubmission]:
    return {
        hotkey: MinerSubmission(
            repo="o/r",
            commit="abc1234",
            cdn_url="https://cdn.example.com",
            revealed_at_block=100,
            round="round1",
        )
    }


def make_tracker(
    mock_git: BuildMockGitHub,
    docker_image_format: str = "registry/{hotkey10}:{tag}",
    timeout_seconds: float = 100,
) -> BuildTracker:
    git_batcher = GitBatcher(git=mock_git, branch="main", base_sha="abc123")
    shutdown = GracefulShutdown()
    mock_git._shutdown = shutdown
    return BuildTracker(
        git_batcher=git_batcher,
        commit_message="submissions for round {current_round}",
        docker_image_format=docker_image_format,
        timeout_seconds=timeout_seconds,
        job_poll_interval_seconds=0,
        run_poll_interval_seconds=0,
        shutdown=shutdown,
    )


class TestTrack:
    async def test_success_build(self) -> None:
        mock_git = BuildMockGitHub()
        mock_git.run_id = 42
        mock_git.jobs = [GitHubJob(id=1, name=JOB_NAME, status="completed", conclusion="success")]
        tracker = make_tracker(mock_git)

        await tracker.track(round_num=1, submissions=make_submission())

        build = tracker._builds[HOTKEY]
        assert build.status == BuildStatus.SUCCESS
        assert build.docker_image is not None
        assert HOTKEY[:10] in build.docker_image

    async def test_failure_build(self) -> None:
        mock_git = BuildMockGitHub()
        mock_git.run_id = 42
        mock_git.jobs = [GitHubJob(id=1, name=JOB_NAME, status="completed", conclusion="failure")]
        tracker = make_tracker(mock_git)

        await tracker.track(round_num=1, submissions=make_submission())

        assert tracker._builds[HOTKEY].status == BuildStatus.FAILURE

    async def test_no_run_found(self) -> None:
        mock_git = BuildMockGitHub()
        mock_git.run_id = None
        tracker = make_tracker(mock_git)

        await tracker.track(round_num=1, submissions=make_submission())

        assert tracker._builds[HOTKEY].status == BuildStatus.PENDING

    async def test_no_matching_job(self) -> None:
        mock_git = BuildMockGitHub()
        mock_git.run_id = 42
        mock_git.jobs = [GitHubJob(id=1, name="Unrelated job", status="completed", conclusion="success")]
        tracker = make_tracker(mock_git)

        await tracker.track(round_num=1, submissions=make_submission())

        assert tracker._builds[HOTKEY].status == BuildStatus.NOT_FOUND

    async def test_timeout(self) -> None:
        mock_git = BuildMockGitHub()
        mock_git.run_id = 42
        mock_git.jobs = [GitHubJob(id=1, name=JOB_NAME, status="in_progress", conclusion=None)]
        tracker = make_tracker(mock_git, timeout_seconds=0)

        await tracker.track(round_num=1, submissions=make_submission())

        assert tracker._builds[HOTKEY].status == BuildStatus.TIMED_OUT

    async def test_saves_builds_on_change(self) -> None:
        mock_git = BuildMockGitHub()
        mock_git.run_id = 42
        mock_git.jobs = [GitHubJob(id=1, name=JOB_NAME, status="completed", conclusion="success")]
        tracker = make_tracker(mock_git)

        await tracker.track(round_num=1, submissions=make_submission())

        committed = mock_git.committed
        builds_path = "rounds/1/builds.json"
        assert builds_path in committed
        saved = json.loads(committed[builds_path])
        assert saved[HOTKEY]["status"] == "success"

    async def test_docker_image_format(self) -> None:
        mock_git = BuildMockGitHub()
        mock_git.run_id = 42
        mock_git.jobs = [GitHubJob(id=1, name=JOB_NAME, status="completed", conclusion="success")]
        tracker = make_tracker(mock_git, docker_image_format="gcr.io/MyProject/{hotkey10}:{tag}")

        await tracker.track(round_num=1, submissions=make_submission())

        image = tracker._builds[HOTKEY].docker_image
        assert image == f"gcr.io/myproject/{HOTKEY[:10]}:abc1234-round1"


class TestWaitForBuild:
    async def test_unblocks_when_build_resolves(self) -> None:
        """wait_for_build blocks until track() fires the event via _notify_resolved."""
        mock_git = BuildMockGitHub()
        mock_git.run_id = 42
        mock_git.jobs = [GitHubJob(id=1, name=JOB_NAME, status="completed", conclusion="success")]
        tracker = make_tracker(mock_git)
        stop = GenerationStop()

        _, build = await asyncio.gather(
            tracker.track(round_num=1, submissions=make_submission()),
            tracker.wait_for_build(hotkey=HOTKEY, generation_stop=stop),
        )

        assert build is not None
        assert build.status == BuildStatus.SUCCESS

    async def test_unblocks_on_shutdown(self) -> None:
        """wait_for_build unblocks via _notify_all when track() exits on shutdown."""
        mock_git = BuildMockGitHub()
        mock_git.run_id = None
        tracker = make_tracker(mock_git)
        stop = GenerationStop()

        _, build = await asyncio.gather(
            tracker.track(round_num=1, submissions=make_submission()),
            tracker.wait_for_build(hotkey=HOTKEY, generation_stop=stop),
        )

        assert build is not None
        assert build.status == BuildStatus.PENDING

    async def test_returns_immediately_when_already_resolved(self) -> None:
        mock_git = BuildMockGitHub()
        mock_git.run_id = 42
        mock_git.jobs = [GitHubJob(id=1, name=JOB_NAME, status="completed", conclusion="success")]
        tracker = make_tracker(mock_git)

        await tracker.track(round_num=1, submissions=make_submission())

        stop = GenerationStop()
        build = await tracker.wait_for_build(hotkey=HOTKEY, generation_stop=stop)
        assert build is not None
        assert build.status == BuildStatus.SUCCESS

    async def test_returns_none_for_unknown_hotkey(self) -> None:
        mock_git = BuildMockGitHub()
        mock_git.run_id = 42
        mock_git.jobs = [GitHubJob(id=1, name=JOB_NAME, status="completed", conclusion="success")]
        tracker = make_tracker(mock_git)
        stop = GenerationStop()

        _, build = await asyncio.gather(
            tracker.track(round_num=1, submissions=make_submission()),
            tracker.wait_for_build(hotkey="unknown", generation_stop=stop),
        )

        assert build is None

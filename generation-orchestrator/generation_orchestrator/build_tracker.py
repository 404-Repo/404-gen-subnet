import asyncio

from loguru import logger
from subnet_common.competition.build_info import BuildInfo, BuildStatus, save_builds
from subnet_common.competition.submissions import MinerSubmission
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubJob
from subnet_common.graceful_shutdown import GracefulShutdown

from generation_orchestrator.generation_stop import GenerationStop


TERMINAL_STATUSES = frozenset(
    {
        BuildStatus.SUCCESS,
        BuildStatus.FAILURE,
        BuildStatus.NOT_FOUND,
        BuildStatus.TIMED_OUT,
    }
)
"""Build statuses that indicate no further progress is expected."""


class BuildTracker:
    """Tracks Docker build jobs and notifies waiters when builds complete."""

    def __init__(
        self,
        git_batcher: GitBatcher,
        commit_message: str,
        docker_image_format: str,
        timeout_seconds: float,
        job_poll_interval_seconds: float,
        run_poll_interval_seconds: float,
        shutdown: GracefulShutdown,
    ) -> None:
        self._git_batcher = git_batcher
        self._commit_message = commit_message
        self._docker_image_format = docker_image_format
        self._timeout_seconds = timeout_seconds
        self._job_poll_interval_seconds = job_poll_interval_seconds
        self._run_poll_interval_seconds = run_poll_interval_seconds
        self._shutdown = shutdown

        self._builds: dict[str, BuildInfo] = {}
        self._build_events: dict[str, asyncio.Event] = {}

    async def track(self, round_num: int, submissions: dict[str, MinerSubmission]) -> None:
        """Track Docker build jobs until all reach the terminal state or timeout.

        Args:
            round_num: Current competition round.
            submissions: Map of hotkey -> tag for miners who submitted.
        """
        self._builds = {hotkey: BuildInfo.from_submission(submission) for hotkey, submission in submissions.items()}

        run_id = await self._wait_for_build_run(round_num=round_num)
        if run_id is None:
            self._notify_all()
            return

        deadline = _now() + self._timeout_seconds

        while not self._shutdown.should_stop:
            jobs = await self._fetch_build_jobs(run_id=run_id)
            changed = self._sync_build_statuses(jobs=jobs)

            if _now() >= deadline:
                changed.update(self._timeout_incomplete())

            if changed:
                await save_builds(
                    git_batcher=self._git_batcher,
                    current_round=round_num,
                    builds=self._builds,
                )
                self._notify_resolved(hotkeys=changed)

            if self._all_resolved():
                break

            await self._shutdown.wait(timeout=self._job_poll_interval_seconds)

        self._notify_all()

    async def wait_for_build(self, hotkey: str, generation_stop: GenerationStop) -> BuildInfo | None:
        """Wait for a build to complete or generation to stop early.

        Args:
            hotkey: The miner hotkey to wait for.
            generation_stop: Stop signal with the wait_for_event method.

        Returns:
            BuildInfo if the build reached terminal state, None if stopped early or not found.
        """
        build = self._builds.get(hotkey)
        if build is not None and build.status in TERMINAL_STATUSES:
            return build

        event = self._get_or_create_event(hotkey)
        await generation_stop.wait_for_event(event)

        return self._builds.get(hotkey)

    def _get_or_create_event(self, hotkey: str) -> asyncio.Event:
        if hotkey not in self._build_events:
            self._build_events[hotkey] = asyncio.Event()
        return self._build_events[hotkey]

    def _notify_resolved(self, hotkeys: set[str]) -> None:
        """Set events for builds that reached the terminal state."""
        for hotkey in hotkeys:
            build = self._builds.get(hotkey)
            if build is not None and build.status in TERMINAL_STATUSES:
                if hotkey in self._build_events:
                    self._build_events[hotkey].set()

    def _notify_all(self) -> None:
        """Set all events to unblock any waiters."""
        for event in self._build_events.values():
            event.set()

    async def _wait_for_build_run(self, round_num: int) -> int | None:
        """Wait for GitHub Actions run with a matching commit message."""
        commit_message = self._commit_message.format(current_round=round_num)

        while not self._shutdown.should_stop:
            run_id = await self._git_batcher.git.find_run_by_commit_message(message=commit_message)
            if run_id is not None:
                return int(run_id)
            await self._shutdown.wait(timeout=self._run_poll_interval_seconds)

        logger.info("Shutdown before build run found")
        return None

    async def _fetch_build_jobs(self, run_id: int) -> dict[str, GitHubJob]:
        """Fetch build jobs keyed by miner hotkey."""
        jobs = await self._git_batcher.git.get_jobs(run_id=run_id)
        return {hotkey: job for job in jobs if (hotkey := _parse_hotkey(job_name=job.name))}

    def _sync_build_statuses(self, jobs: dict[str, GitHubJob]) -> set[str]:
        """Update builds from jobs, return changed hotkeys."""
        changed = set()

        for hotkey, build in self._builds.items():
            if build.status in TERMINAL_STATUSES:
                continue

            new_status = _derive_status(job=jobs.get(hotkey))
            if new_status == build.status:
                continue

            build.status = new_status
            if new_status == BuildStatus.SUCCESS:
                build.docker_image = self._docker_image_format.format(hotkey10=hotkey[:10], tag=build.tag).lower()
            changed.add(hotkey)

        return changed

    def _timeout_incomplete(self) -> set[str]:
        """Mark pending/in-progress builds as timed out."""
        changed = set()
        for hotkey, build in self._builds.items():
            if build.status in (BuildStatus.PENDING, BuildStatus.IN_PROGRESS):
                logger.warning("Build %s timed out", hotkey)
                build.status = BuildStatus.TIMED_OUT
                changed.add(hotkey)
        return changed

    def _all_resolved(self) -> bool:
        """Check if all builds have reached a terminal state."""
        return all(b.status in TERMINAL_STATUSES for b in self._builds.values())


def _parse_hotkey(job_name: str) -> str | None:
    """Extract hotkey from job name like 'Bake and push images to Google Cloud (HOTKEY)'."""
    prefix = "Bake and push images to Google Cloud ("
    suffix = ")"
    if job_name.startswith(prefix) and job_name.endswith(suffix):
        return job_name[len(prefix) : -len(suffix)]
    return None


def _derive_status(job: GitHubJob | None) -> BuildStatus:
    """Determine build status from the job state."""
    if job is None:
        return BuildStatus.NOT_FOUND
    if job.conclusion == "success":
        return BuildStatus.SUCCESS
    if job.conclusion in ("failure", "cancelled", "skipped", "timed_out", "action_required"):
        return BuildStatus.FAILURE
    if job.status == "in_progress":
        return BuildStatus.IN_PROGRESS
    return BuildStatus.PENDING


def _now() -> float:
    """Current event loop time."""
    return asyncio.get_running_loop().time()

import json
from datetime import UTC, datetime

import httpx
import pytest
from loguru import logger
from pydantic import ValidationError
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.testing import MockGitHubClient

from validation_service.execution import ValidationJob
from validation_service.settings import Settings
from validation_service.validation_iteration import validation_iteration


COMMIT = "a" * 40
NEXT_COMMIT = "b" * 40
ETA = datetime(2026, 11, 3, tzinfo=UTC)


@pytest.fixture
def files() -> dict[str, str]:
    return {
        "state.json": CompetitionState(current_round=7, stage=RoundStage.DUELS, next_stage_eta=ETA).model_dump_json(),
        "rounds/7/validation_requests.json": json.dumps(
            [{"hotkey": "pending"}, {"hotkey": "passed"}, {"hotkey": "failed"}, {"hotkey": "pending"}]
        ),
        "rounds/7/validation_results.json": json.dumps(
            {"passed": {"verdict": "passed"}, "failed": {"verdict": "failed", "reason": "Output differs"}}
        ),
    }


async def test_uses_one_pinned_snapshot_when_branch_moves(settings: Settings, files: dict[str, str]) -> None:
    branch_commit = COMMIT
    snapshots = {
        COMMIT: files,
        NEXT_COMMIT: {
            "state.json": CompetitionState(current_round=8, stage=RoundStage.DUELS).model_dump_json(),
            "rounds/8/validation_requests.json": '[{"hotkey": "new-round"}]',
        },
    }

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal branch_commit
        assert request.method == "GET"
        if request.url.path == "/repos/test/competition/commits/competition":
            resolved = branch_commit
            branch_commit = NEXT_COMMIT
            return httpx.Response(200, json={"sha": resolved})
        path = request.url.path.removeprefix("/repos/test/competition/contents/")
        ref = request.url.params["ref"]
        snapshot = snapshots[branch_commit if ref == "competition" else ref]
        content = snapshot.get(path)
        return httpx.Response(200, text=content) if content is not None else httpx.Response(404)

    jobs: list[ValidationJob] = []

    async def execute_job(job: ValidationJob) -> None:
        jobs.append(job)

    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(respond)) as client:
        git = GitHubClient.with_client(client, repo=settings.github_repo)
        eta = await validation_iteration(git, settings, GracefulShutdown(), execute_job)
        assert jobs == [ValidationJob(7, "pending", COMMIT)]
        assert eta == ETA

        await validation_iteration(git, settings, GracefulShutdown(), execute_job)
        assert jobs[-1] == ValidationJob(8, "new-round", NEXT_COMMIT)


async def test_repeated_polls_and_restart_log_pending_job_without_writes(
    settings: Settings, files: dict[str, str]
) -> None:
    original_files = files.copy()
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(message.record["message"]))
    try:
        for _ in range(2):
            git = MockGitHubClient(files=files, ref_sha=COMMIT)
            shutdown = GracefulShutdown()
            for _ in range(2):
                await validation_iteration(git, settings, shutdown)
            assert git.committed == {}
    finally:
        logger.remove(sink)

    ready = [message for message in messages if message.startswith("Validation job ready:")]
    assert ready == [f"Validation job ready: round=7, hotkey=pending, competition_state_commit={COMMIT}"] * 4
    assert files == original_files


@pytest.mark.parametrize("requests", [None, "[]"])
async def test_missing_or_empty_requests_mean_no_work(
    settings: Settings, files: dict[str, str], requests: str | None
) -> None:
    if requests is None:
        del files["rounds/7/validation_requests.json"]
    else:
        files["rounds/7/validation_requests.json"] = requests
    git = MockGitHubClient(files=files, ref_sha=COMMIT)
    jobs: list[ValidationJob] = []

    async def execute_job(job: ValidationJob) -> None:
        jobs.append(job)

    assert await validation_iteration(git, settings, GracefulShutdown(), execute_job) == ETA
    assert jobs == []
    assert git.committed == {}


async def test_missing_results_leave_all_requested_candidates_pending(
    settings: Settings, files: dict[str, str]
) -> None:
    del files["rounds/7/validation_results.json"]
    git = MockGitHubClient(files=files, ref_sha=COMMIT)
    jobs: list[ValidationJob] = []

    async def execute_job(job: ValidationJob) -> None:
        jobs.append(job)

    await validation_iteration(git, settings, GracefulShutdown(), execute_job)
    assert jobs == [ValidationJob(7, hotkey, COMMIT) for hotkey in ("pending", "passed", "failed")]


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("state.json", "not json"),
        ("state.json", '{"current_round": 7, "stage": "invalid"}'),
        ("rounds/7/validation_requests.json", "not json"),
        ("rounds/7/validation_requests.json", '[{"hotkey": "pending", "defender": "other"}]'),
        ("rounds/7/validation_results.json", "not json"),
        ("rounds/7/validation_results.json", '{"pending": {"verdict": "failed", "reason": ""}}'),
        ("rounds/7/validation_results.json", '{"pending": {"verdict": "pending"}}'),
    ],
)
async def test_bad_data_is_a_service_error_before_execution(
    settings: Settings, files: dict[str, str], path: str, content: str
) -> None:
    files[path] = content
    git = MockGitHubClient(files=files, ref_sha=COMMIT)
    jobs: list[ValidationJob] = []

    async def execute_job(job: ValidationJob) -> None:
        jobs.append(job)

    with pytest.raises(ValidationError):
        await validation_iteration(git, settings, GracefulShutdown(), execute_job)
    assert jobs == []
    assert git.committed == {}


async def test_missing_state_is_a_service_error(settings: Settings) -> None:
    git = MockGitHubClient()
    with pytest.raises(FileNotFoundError, match="state.json"):
        await validation_iteration(git, settings, GracefulShutdown())
    assert git.committed == {}


@pytest.mark.parametrize(
    "failing_path",
    [
        "/repos/test/competition/commits/competition",
        "/repos/test/competition/contents/state.json",
        "/repos/test/competition/contents/rounds/7/validation_requests.json",
        "/repos/test/competition/contents/rounds/7/validation_results.json",
    ],
)
async def test_github_errors_remain_service_errors(
    settings: Settings, files: dict[str, str], failing_path: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path == failing_path:
            return httpx.Response(503)
        if request.url.path.endswith("/commits/competition"):
            return httpx.Response(200, json={"sha": COMMIT})
        return httpx.Response(200, text=files[request.url.path.removeprefix("/repos/test/competition/contents/")])

    jobs: list[ValidationJob] = []

    async def execute_job(job: ValidationJob) -> None:
        jobs.append(job)

    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(respond)) as client:
        git = GitHubClient.with_client(client, repo=settings.github_repo)
        with pytest.raises(httpx.HTTPStatusError):
            await validation_iteration(git, settings, GracefulShutdown(), execute_job)
    assert jobs == []


async def test_shutdown_stops_before_next_job(settings: Settings, files: dict[str, str]) -> None:
    del files["rounds/7/validation_results.json"]
    shutdown = GracefulShutdown()
    jobs: list[ValidationJob] = []

    async def execute_job(job: ValidationJob) -> None:
        jobs.append(job)
        shutdown.request_shutdown()

    await validation_iteration(MockGitHubClient(files=files, ref_sha=COMMIT), settings, shutdown, execute_job)
    assert jobs == [ValidationJob(7, "pending", COMMIT)]

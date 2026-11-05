from collections.abc import Awaitable, Callable
from datetime import datetime

from loguru import logger
from subnet_common.competition.state import require_state
from subnet_common.competition.validation import get_validation_results
from subnet_common.competition.validation_requests import get_validation_requests
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from validation_service.execution import ValidationJob, execute_validation_job
from validation_service.settings import Settings


async def run_validation_iteration(settings: Settings, shutdown: GracefulShutdown) -> datetime | None:
    """Create I/O dependencies and run one validation cycle."""
    async with GitHubClient(
        repo=settings.github_repo,
        token=settings.github_token.get_secret_value(),
    ) as git:
        return await validation_iteration(git=git, settings=settings, shutdown=shutdown)


async def validation_iteration(
    git: GitHubClient,
    settings: Settings,
    shutdown: GracefulShutdown,
    execute_job: Callable[[ValidationJob], Awaitable[None]] = execute_validation_job,
) -> datetime | None:
    """Read one competition snapshot and hand pending candidates to execution."""
    ref = await git.get_ref_sha(ref=settings.github_branch)
    state = await require_state(git=git, ref=ref)
    logger.info(f"Commit: {ref[:10]}, state: {state}")

    requests = await get_validation_requests(git=git, round_num=state.current_round, ref=ref)
    results = await get_validation_results(git=git, round_num=state.current_round, ref=ref)

    for request in requests:
        if shutdown.should_stop:
            break
        if request.hotkey in results:
            continue
        await execute_job(
            ValidationJob(round_num=state.current_round, hotkey=request.hotkey, competition_state_commit=ref)
        )

    return state.next_stage_eta

import httpx
from loguru import logger
from openai import AsyncOpenAI
from subnet_common.competition.state import RoundStage, require_state
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.match_runner import MatchRunner
from judge_service.settings import Settings


async def run_judge_iteration(settings: Settings, shutdown: GracefulShutdown) -> None:
    """Entry point: creates I/O dependencies and delegates to judge_iteration."""
    async with GitHubClient(
        repo=settings.github_repo,
        token=settings.github_token.get_secret_value(),
    ) as git:
        openai = _create_openai_client(settings)
        await judge_iteration(git=git, openai=openai, settings=settings, shutdown=shutdown)


async def judge_iteration(
    git: GitHubClient,
    openai: AsyncOpenAI,
    settings: Settings,
    shutdown: GracefulShutdown,
) -> None:
    """Run one judge cycle.

    Loads state from Git, delegates to MatchRunner if in Duels stage,
    and persists results. Stops early if the round winner is verified.
    """
    ref = await git.get_ref_sha(ref=settings.github_branch)
    state = await require_state(git, ref=ref)
    logger.info(f"Commit: {ref[:10]}, state: {state}")

    if state.stage != RoundStage.DUELS:
        return

    git_batcher = await GitBatcher.create(git=git, branch=settings.github_branch, base_sha=ref)
    runner = await MatchRunner.create(
        git_batcher=git_batcher,
        state=state,
        openai=openai,
        settings=settings,
    )
    await runner.run(shutdown)


def _create_openai_client(settings: Settings) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        http_client=httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)),
    )

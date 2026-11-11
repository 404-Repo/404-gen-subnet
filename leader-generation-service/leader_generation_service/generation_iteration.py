import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from bittensor_wallet import Wallet
from loguru import logger
from pod_gateway.membership import Membership, MembershipRefresher
from pod_gateway.runpod import RunPodClient
from subnet_common.competition.config import require_competition_config
from subnet_common.competition.prompts import prompt_ids, require_prompts
from subnet_common.competition.schedule import require_schedule
from subnet_common.competition.state import RoundStage, require_state
from subnet_common.discord import DiscordWebhook
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from .credentials import CredentialManager
from .round_runner import RoundRunner, Runner
from .runpod import RunpodRunner
from .settings import Settings
from .witness import SavedResults


@dataclass
class Dependencies:
    git: GitHubClient
    runner: Runner
    credentials: CredentialManager
    saved: SavedResults
    head: Callable[[], Awaitable[tuple[int, float]]]
    hotkey: str
    discord: DiscordWebhook | None = None


@asynccontextmanager
async def create_dependencies(settings: Settings, shutdown: GracefulShutdown) -> AsyncIterator[Dependencies]:
    wallet = Wallet(name=settings.wallet_name, hotkey=settings.wallet_hotkey, path=str(settings.wallet_path))
    if wallet.hotkey_file.is_encrypted():
        raise ValueError("Unattended leader generation requires a dedicated unencrypted hotkey")
    key = wallet.hotkey
    async with GitHubClient(repo=settings.github_repo, token=settings.github_token.get_secret_value()) as git:
        async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as http:
            with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as provider_http:
                provider = RunPodClient(settings.runpod_api_key.get_secret_value(), provider_http, min_interval=1.1)
                membership = Membership(17, "leader-service")
                chain = MembershipRefresher(membership, provider_http, settings.subtensor_rpc_url)

                async def head() -> tuple[int, float]:
                    return await asyncio.to_thread(chain.generation_head)

                yield Dependencies(
                    git,
                    RunpodRunner(settings, provider, provider_http, shutdown=shutdown),
                    CredentialManager(settings.codex_home, settings.codex),
                    SavedResults(http, settings.cdn_url),
                    head,
                    key.ss58_address,
                    discord=(
                        DiscordWebhook(settings.discord_webhook_url.get_secret_value())
                        if settings.discord_webhook_url is not None
                        else None
                    ),
                )


async def generation_iteration(
    controller: RoundRunner,
    settings: Settings,
) -> None:
    dependencies, shutdown = controller.dependencies, controller.shutdown
    git = dependencies.git
    ref = await git.get_ref_sha(settings.github_branch)
    state = await require_state(git, ref)
    number = state.current_round
    logger.info("Competition {} at {}: {}", number, ref[:10], state.stage.value)
    if shutdown.should_stop:
        return
    await controller.select_round(number)
    if state.stage != RoundStage.MINER_GENERATION:
        logger.info("Round {} skipped because stage is {}", number, state.stage.value)
        await controller.stop()
        return
    logger.info("Round {} loading competition settings", number)
    config = await require_competition_config(git, ref)
    if config.leader_generation_hotkey is None:
        logger.info("Round {} skipped because config.json has no leader_generation_hotkey", number)
        await controller.stop()
        return
    if config.leader_generation_hotkey != dependencies.hotkey:
        logger.error("Round {} configured hotkey differs from the operator wallet", number)
        await controller.stop()
        raise ValueError("Configured leader generation hotkey differs from the operator wallet")
    schedule = await require_schedule(git, number, ref)
    if not schedule.witness_save_submissions:
        await controller.stop()
        raise ValueError("Leader generation requires witness result saving")
    deadline = schedule.generation_deadline_block
    if deadline is not None:
        logger.info("Round {} checking the generation deadline on Finney", number)
        if await controller.deadline_reached(deadline):
            logger.info("Round {} skipped because the generation deadline has been reached", number)
            await controller.stop()
            return
    expected = set(prompt_ids(await require_prompts(git, number, ref)))
    logger.info("Round {} checking saved witness results", number)
    saved = await dependencies.saved.read(
        f"{config.name}-{number}", schedule.witness_storage_prefix, dependencies.hotkey
    )
    completed = sum(saved[prompt].is_terminal() for prompt in expected & saved.keys())
    await controller.run(number, completed, len(expected), config.generation_stage_minutes, ref, deadline)


async def maintain_credentials(dependencies: Dependencies, settings: Settings, shutdown: GracefulShutdown) -> None:
    while not shutdown.should_stop:
        try:
            await dependencies.credentials.current()
        except Exception as error:
            logger.error("Subscription maintenance failed ({})", type(error).__name__)
        await shutdown.wait(timeout=settings.credential_check_interval_seconds)

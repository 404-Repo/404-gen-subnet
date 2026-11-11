import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from bittensor_wallet import Wallet
from loguru import logger
from miner_runtime.pod import PARAMS_PATH
from pod_gateway.membership import Membership, MembershipRefresher
from pod_gateway.runpod import RunPodClient
from subnet_common.competition.config import require_competition_config
from subnet_common.competition.leader import require_leader_state
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.schedule import require_schedule
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.state import RoundStage, require_state
from subnet_common.discord import DiscordWebhook
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.witness.rounds import RoundInputs, repository

from .credentials import CredentialManager
from .models import Job, Snapshot
from .round_runner import RoundRunner, Runner, prompt_ids
from .runpod import RunpodRunner
from .runtime import ROOT
from .settings import Settings
from .store import JobStore
from .witness import SavedResults, WitnessInputs


@dataclass
class Dependencies:
    git: GitHubClient
    store: JobStore
    runner: Runner
    credentials: CredentialManager
    inputs: Callable[[], Awaitable[RoundInputs]]
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
    store = JobStore(settings.state_directory)
    async with GitHubClient(repo=settings.github_repo, token=settings.github_token.get_secret_value()) as git:
        async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as http:
            with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as provider_http:
                provider = RunPodClient(settings.runpod_api_key.get_secret_value(), provider_http, min_interval=1.1)
                membership = Membership(17, "leader-service")
                chain = MembershipRefresher(membership, provider_http, settings.subtensor_rpc_url)

                async def head() -> tuple[int, float]:
                    return await asyncio.to_thread(chain.generation_head)

                witness = WitnessInputs(settings.witness_url, key, ROOT / PARAMS_PATH)
                yield Dependencies(
                    git,
                    store,
                    RunpodRunner(settings, store, provider, provider_http, shutdown=shutdown),
                    CredentialManager(settings.codex_home, settings.codex),
                    witness.read,
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
    git, store = dependencies.git, dependencies.store
    ref = await git.get_ref_sha(settings.github_branch)
    state = await require_state(git, ref)
    logger.info("Competition {} at {}: {}", state.current_round, ref[:10], state.stage.value)
    jobs = sorted(store.jobs(), key=lambda job: job.snapshot.inputs.number)
    current = None
    for job in jobs:
        inputs = job.snapshot.inputs
        if (
            inputs.repository != repository(settings.github_repo)
            or inputs.branch != settings.github_branch
            or job.snapshot.hotkey != dependencies.hotkey
            or job.snapshot.witness_url != settings.witness_url
        ):
            raise ValueError("Job belongs to another competition, witness or operator identity")
        if shutdown.should_stop:
            return None
        if inputs.number != state.current_round:
            await dependencies.runner.terminate(job)
            continue
        current = job
    if current is not None:
        await controller.run(current)
        return
    if state.stage != RoundStage.MINER_GENERATION:
        logger.info("Round {} skipped because stage is {}", state.current_round, state.stage.value)
        return
    config = await require_competition_config(git, ref)
    if config.leader_generation_hotkey is None:
        logger.info("Round {} skipped because config.json has no leader_generation_hotkey", state.current_round)
        return
    if config.leader_generation_hotkey != dependencies.hotkey:
        raise ValueError("Configured leader generation hotkey differs from the operator wallet")
    schedule = await require_schedule(git, state.current_round, ref)
    urls = await require_prompts(git, state.current_round, ref)
    seed = await require_seed_from_git(git, state.current_round, ref)
    leader = (await require_leader_state(git, ref)).get_latest()
    if leader is None:
        raise ValueError("Competition has no incumbent leader")
    if not schedule.witness_save_submissions:
        raise ValueError("Leader generation requires witness result saving")
    if schedule.generation_deadline_block is not None:
        # Avoid even a witness allocation when the published deadline has closed.
        block, observed = await dependencies.head()
        if not time.time() - 60 <= observed <= time.time() + 5:
            raise ValueError("Generation block is stale")
        if block >= schedule.generation_deadline_block:
            logger.info("Round {} skipped because the generation deadline has been reached", state.current_round)
            return
    credentials = await dependencies.credentials.current()
    inputs = await dependencies.inputs()
    if (
        inputs.repository != repository(settings.github_repo)
        or inputs.branch != settings.github_branch
        or inputs.number != state.current_round
        or inputs.round != f"{config.name}-{state.current_round}"
        or inputs.seed != seed
        or set(inputs.prompt_ids.values()) != prompt_ids(urls)
        or inputs.storage_prefix != schedule.witness_storage_prefix
        or inputs.generation_deadline_block != schedule.generation_deadline_block
        or not inputs.save_submissions
    ):
        raise ValueError("Witness frozen inputs differ from the published round")
    snapshot = Snapshot(
        state_ref=ref,
        generation_image=settings.generation_image,
        leader=leader,
        inputs=inputs,
        hotkey=dependencies.hotkey,
        account_id=credentials.account_id,
        witness_url=settings.witness_url,
        wallet_name=settings.wallet_name,
        wallet_hotkey=settings.wallet_hotkey,
        workers=settings.workers,
        parallel_calls=settings.parallel_calls,
        parallel_preparations=settings.parallel_preparations,
        prover_contexts=settings.prover_contexts,
        go_cpus=settings.go_cpus,
        go_memory_mib=settings.go_memory_mib,
    )
    job = Job(snapshot=snapshot)
    store.save(job)
    await controller.run(job)


async def maintain_credentials(dependencies: Dependencies, settings: Settings, shutdown: GracefulShutdown) -> None:
    while not shutdown.should_stop:
        try:
            await dependencies.credentials.current()
        except Exception as error:
            logger.error("Subscription maintenance failed ({})", type(error).__name__)
        await shutdown.wait(timeout=settings.credential_check_interval_seconds)

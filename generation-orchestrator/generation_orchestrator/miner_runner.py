import asyncio
from enum import Enum

from loguru import logger
from subnet_common.competition.audit_requests import AuditRequest
from subnet_common.competition.generation_report import GenerationReport, GenerationReportOutcome
from subnet_common.competition.generations import (
    GenerationResult,
    GenerationSource,
    get_generations,
    save_generations,
)
from subnet_common.competition.pod_config import PodConfig, resolve_providers, runpod_allowed_cuda_versions
from subnet_common.competition.pod_stats import PodStats, save_pod_stats
from subnet_common.git_batcher import GitBatcher
from subnet_common.render import warmup as render_warmup

from generation_orchestrator.discord import NULL_DISCORD_NOTIFIER, DiscordNotifier
from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.generation_summary import summarize_generation
from generation_orchestrator.gpu_provider import DeployedContainer, GPUProviderManager
from generation_orchestrator.gpu_provider.common import GPUProvider
from generation_orchestrator.pod_session import PodReplaceRequested, PodSession, ReplaceReason
from generation_orchestrator.prompts import Prompt
from generation_orchestrator.settings import Settings
from generation_orchestrator.staggered_semaphore import StaggeredSemaphore


_TERMINATION_COMPLETED = "completed"

_PROVIDER_BY_NAME: dict[str, GPUProvider] = {
    "targon": GPUProvider.TARGON,
    "verda": GPUProvider.VERDA,
    "runpod": GPUProvider.RUNPOD,
}


class _Verdict(Enum):
    """Internal outcome of a miner run."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    DEPLOY_FAILURE = "deploy_failure"
    EARLY_STOP = "early_stop"


class MinerRunner:
    """Full lifecycle for one hotkey (miner or leader) in a round: deploy a pod,
    run batches on it, handle replacements, collect results, and produce a generation
    report.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        semaphore: StaggeredSemaphore,
        gpu_manager: GPUProviderManager,
        git_batcher: GitBatcher,
        hotkey: str,
        docker_image: str,
        prompts: list[Prompt],
        current_round: int,
        seed: int,
        stop: GenerationStop,
        audit_repeats: int = 1,
        audit_request: AuditRequest | None = None,
        discord: DiscordNotifier = NULL_DISCORD_NOTIFIER,
        pod_config: PodConfig | None = None,
    ):
        self._settings = settings
        self._semaphore = semaphore
        self._gpu_manager = gpu_manager
        self._git_batcher = git_batcher
        self._hotkey = hotkey
        self._docker_image = docker_image
        self._prompts = prompts
        self._current_round = current_round
        self._seed = seed
        self._stop = stop
        self._audit_repeats = audit_repeats if audit_request is not None else 1
        self._audit_request = audit_request
        self._discord = discord
        self._pod_config = pod_config

        self._log_id = hotkey[:10]
        self._miner_prefix = f"miner-{self._current_round:02d}-{self._hotkey[:10].lower()}"

        self._submitted_stems: set[str] = set()
        self._generations_by_repeat: dict[int, dict[str, GenerationResult]] = {
            r: {} for r in range(1, self._audit_repeats + 1)
        }
        self._replacements_used: int = 0
        self._generation_time_by_repeat: dict[int, float] = {r: 0.0 for r in range(1, self._audit_repeats + 1)}
        self._pod_stats: dict[str, PodStats] = {}

    async def run(self) -> GenerationReport | None:
        """Run generation for one miner.

        Returns:
            GenerationReport - generation completed or rejected
            None - generation-only run, or interrupted (should retry later)
        """
        async with self._semaphore:
            if self._stop.should_stop:
                return None
            return await self._run()

    async def _run(self) -> GenerationReport | None:
        """Load state, deploy pods with replacement loop, return audit result."""
        if self._audit_request is not None:
            all_submitted = await get_generations(
                git=self._git_batcher.git,
                hotkey=self._hotkey,
                round_num=self._current_round,
                source=GenerationSource.SUBMITTED,
                ref=self._settings.github_branch,
            )
            self._submitted_stems = {k for k, v in all_submitted.items() if v.js is not None}

        for repeat in range(1, self._audit_repeats + 1):
            prior = await get_generations(
                git=self._git_batcher.git,
                hotkey=self._hotkey,
                round_num=self._current_round,
                source=GenerationSource.GENERATED,
                ref=self._settings.github_branch,
                repeat_index=repeat,
            )
            self._generations_by_repeat[repeat] = dict(prior)

        if self._all_done():
            logger.info(
                f"{self._log_id}: all {len(self._target_prompts())} prompts already done "
                f"across {self._audit_repeats} repeat(s)"
            )
            return self._build_report(_Verdict.COMPLETED)

        logger.info(f"{self._log_id}: {self._pending_count()} prompts to process")

        # Fire-and-forget: wake a possibly-cold serverless render service now, so it's up
        # by the time the first batch finishes, and we need to render. Bounded by its own
        # timeout; failures are harmless — render() has its own retry budget.
        render_key = self._settings.render_api_key.get_secret_value() if self._settings.render_api_key else None
        asyncio.create_task(
            render_warmup(
                endpoint=self._settings.render_service_url,
                timeout=self._settings.render_warmup_timeout_seconds,
                api_key=render_key,
                log_id=self._log_id,
            )
        )

        try:
            verdict = await self._run_with_replacements()
            await self._send_progress()
            logger.info(f"{self._log_id}: finished with verdict={verdict.value}")
            return self._build_report(verdict)
        finally:
            await self._cleanup_all()

    async def _run_with_replacements(self) -> _Verdict:
        """Deploy pods and run batches, replacing on crash or miner request."""
        warmup = await self._warmup_initial_pod()
        if warmup is None:
            return _Verdict.PARTIAL if self._stop.should_stop else _Verdict.DEPLOY_FAILURE
        pod_index, deployed = warmup

        while not self._stop.should_stop:
            swap = await self._run_pod(deployed, pod_index)

            if swap is None:
                return self._determine_verdict()

            await self._notify_swap(swap.reason, pod_index)

            if not self._consume_replacement(swap.reason.value):
                return self._determine_verdict()

            pod_index, next_deployed = await self._deploy_next_pod(pod_index + 1)
            if next_deployed is None:
                return _Verdict.PARTIAL if self._stop.should_stop else self._determine_verdict()
            deployed = next_deployed

        return _Verdict.PARTIAL

    async def _notify_swap(self, reason: ReplaceReason, pod_index: int) -> None:
        """Surface infra-side pod failures to Discord. REQUESTED and BATCH_TIME_LIMIT
        are intentionally silent — the first is a miner-side decision, the second is
        already covered by the eventual generation report."""
        pod_id = f"{self._miner_prefix}-{pod_index}"
        if reason == ReplaceReason.SUBMISSION_FAILED:
            await self._discord.notify_batch_submit_failed(self._hotkey, pod_id)
        elif reason == ReplaceReason.DOWNLOAD_FAILED:
            await self._discord.notify_batch_download_failed(self._hotkey, pod_id)
        elif reason == ReplaceReason.UNREACHABLE:
            await self._discord.notify_pod_unreachable(self._hotkey, pod_id)

    async def _run_pod(self, deployed: DeployedContainer, pod_index: int) -> PodReplaceRequested | None:
        """Run all pending repeats on one pod. Returns a swap signal or None if nothing to swap for.

        For an audit miner each of `audit_repeats` runs the full target prompt set on this
        same pod. Per-(pod, repeat) batch times and termination reasons are recorded as
        separate PodStats rows keyed `f"{pod_id}-r{repeat}"`.
        """
        pod_id = f"{self._miner_prefix}-{pod_index}"
        per_repeat_batch_times: dict[int, list[float]] = {}
        per_repeat_pod_total: dict[int, float] = {}
        swap: PodReplaceRequested | None = None
        last_repeat: int = 1

        try:
            async with PodSession(
                settings=self._settings,
                pod_endpoint=deployed.info.url,
                auth_token=deployed.generation_token,
                hotkey=self._hotkey,
                seed=self._seed,
                stop=self._stop,
                remaining_replacements=self._settings.max_replacements - self._replacements_used,
            ) as session:
                for repeat in range(1, self._audit_repeats + 1):
                    last_repeat = repeat
                    pending = [p for p in self._target_prompts() if p.stem not in self._generations_by_repeat[repeat]]
                    if not pending:
                        continue

                    per_repeat_batch_times[repeat] = []
                    per_repeat_pod_total[repeat] = 0.0

                    for batch in self._chunk(pending):
                        if self._stop.should_stop:
                            return swap

                        outcome = await session.run(self._current_round, batch, repeat_index=repeat)
                        if isinstance(outcome, PodReplaceRequested):
                            swap = outcome
                            return swap

                        self._generations_by_repeat[repeat].update(outcome.generations)
                        await save_generations(
                            git_batcher=self._git_batcher,
                            hotkey=self._hotkey,
                            round_num=self._current_round,
                            generations=self._generations_by_repeat[repeat],
                            source=GenerationSource.GENERATED,
                            repeat_index=repeat,
                        )
                        per_repeat_batch_times[repeat].append(outcome.batch_time)
                        per_repeat_pod_total[repeat] += outcome.batch_time
                        self._generation_time_by_repeat[repeat] += outcome.batch_time
        finally:
            for repeat, times in per_repeat_batch_times.items():
                if repeat == last_repeat and swap is not None:
                    reason = swap.reason.value
                    payload = swap.payload
                else:
                    reason = _TERMINATION_COMPLETED
                    payload = None
                self._pod_stats[f"{pod_id}-r{repeat}"] = PodStats(
                    pod_id=pod_id,
                    repeat_index=repeat,
                    provider=deployed.info.provider.value,
                    batch_times=times,
                    total_generation_time=per_repeat_pod_total[repeat],
                    termination_reason=reason,
                    payload=payload,
                )
            if per_repeat_batch_times:
                await save_pod_stats(
                    git_batcher=self._git_batcher,
                    round_num=self._current_round,
                    hotkey=self._hotkey,
                    stats=self._pod_stats,
                )
            await self._gpu_manager.delete_container(deployed.info)

        return swap

    async def _warmup_initial_pod(self) -> tuple[int, DeployedContainer] | None:
        """Deploy the first healthy pod, retrying up to max_initial_deploy_attempts.

        Returns (pod_index, deployed) on success; None if all attempts failed or stop fired.
        """
        max_attempts = self._settings.max_initial_deploy_attempts
        for attempt in range(1, max_attempts + 1):
            if self._stop.should_stop:
                return None
            pod_index = attempt - 1
            deployed = await self._deploy_and_wait_healthy(f"{self._miner_prefix}-{pod_index}", attempt_index=pod_index)
            if deployed is not None:
                return pod_index, deployed
            if self._stop.should_stop:
                return None  # type: ignore[unreachable]  # property can flip across the await
            if attempt < max_attempts:
                logger.info(f"{self._log_id}: initial deploy failed, retry {attempt}/{max_attempts}")
        logger.warning(f"{self._log_id}: initial deploy failed after {max_attempts}/{max_attempts} attempts")
        return None

    async def _deploy_next_pod(self, start_index: int) -> tuple[int, DeployedContainer | None]:
        """Deploy a replacement pod, consuming one replacement per failed attempt.

        Returns (final_pod_index, deployed); deployed is None when the budget is exhausted
        or stop fires.
        """
        pod_index = start_index
        while not self._stop.should_stop:
            deployed = await self._deploy_and_wait_healthy(f"{self._miner_prefix}-{pod_index}", attempt_index=pod_index)
            if deployed is not None:
                return pod_index, deployed
            if self._stop.should_stop:
                return pod_index, None  # type: ignore[unreachable]  # property can flip across the await
            if not self._consume_replacement("deploy_failed"):
                return pod_index, None
            pod_index += 1
        return pod_index, None

    def _consume_replacement(self, reason: str) -> bool:
        """Try to consume one replacement from the budget. Returns False when exhausted."""
        if self._replacements_used >= self._settings.max_replacements:
            logger.warning(
                f"{self._log_id}: {reason}, replacement limit reached "
                f"({self._replacements_used}/{self._settings.max_replacements})"
            )
            return False
        self._replacements_used += 1
        logger.info(
            f"{self._log_id}: {reason}, " f"replacement {self._replacements_used}/{self._settings.max_replacements}"
        )
        return True

    def _chunk(self, prompts: list[Prompt]) -> list[list[Prompt]]:
        size = self._settings.batch_size
        return [prompts[i : i + size] for i in range(0, len(prompts), size)]

    def _target_prompts(self) -> list[Prompt]:
        """Prompts this miner needs to regenerate.

        In audit mode that's only what they submitted; in generation-only mode it's all of them.
        """
        if self._audit_request is not None:
            return [p for p in self._prompts if p.stem in self._submitted_stems]
        return self._prompts

    def _all_done(self) -> bool:
        targets = self._target_prompts()
        return all(
            all(p.stem in self._generations_by_repeat[r] for p in targets) for r in range(1, self._audit_repeats + 1)
        )

    def _pending_count(self) -> int:
        return sum(
            1
            for r in range(1, self._audit_repeats + 1)
            for p in self._target_prompts()
            if p.stem not in self._generations_by_repeat[r]
        )

    def _build_report(self, verdict: _Verdict) -> GenerationReport | None:
        """Build generation report based on the verdict."""
        if self._audit_request is None:
            return None

        if verdict == _Verdict.PARTIAL:
            return None

        if verdict == _Verdict.DEPLOY_FAILURE:
            return GenerationReport(
                hotkey=self._hotkey,
                outcome=GenerationReportOutcome.REJECTED,
                reason="Failed to deploy a docker image",
            )

        return summarize_generation(
            hotkey=self._hotkey,
            submitted_stems=self._submitted_stems,
            generations_by_repeat=self._generations_by_repeat,
            generation_time_by_repeat=self._generation_time_by_repeat,
            settings=self._settings,
        )

    async def _send_progress(self) -> None:
        prompts = self._target_prompts()
        per_repeat = [self._generations_by_repeat[r] for r in range(1, self._audit_repeats + 1)]
        generated = sum(1 for gens in per_repeat for p in prompts if p.stem in gens and not gens[p.stem].is_failed())
        fails = sum(1 for gens in per_repeat for p in prompts if p.stem in gens and gens[p.stem].is_failed())
        total_time = sum(self._generation_time_by_repeat.values())

        await self._discord.notify_generation_progress(
            hotkey=self._hotkey,
            round_num=self._current_round,
            generated=generated,
            total=len(prompts) * self._audit_repeats,
            fails=fails,
            total_generation_time=total_time,
            replacements_used=self._replacements_used,
        )

    def _determine_verdict(self) -> _Verdict:
        if self._all_done():
            return _Verdict.COMPLETED
        if self._stop.should_stop:
            return _Verdict.PARTIAL
        return _Verdict.EARLY_STOP

    async def _deploy_and_wait_healthy(self, pod_id: str, attempt_index: int) -> DeployedContainer | None:
        """Deploy a pod and wait for it to reach `/status=ready`.

        `attempt_index` drives provider rotation — every successive deploy attempt for
        this miner starts the round-robin at the next provider, so after a working pod
        on provider A (attempt N), the replacement attempt (N+1) tries provider B first.
        """
        logger.debug(f"{pod_id}: deploying")

        cfg = self._pod_config or PodConfig()
        orch = [p.strip() for p in self._settings.gpu_providers.split(",") if p.strip()]
        names = resolve_providers(cfg, orch)
        provider_sequence = [_PROVIDER_BY_NAME[n] for n in names]
        allowed_cuda = runpod_allowed_cuda_versions(cfg.filters.cuda)

        deployed = await self._gpu_manager.get_healthy_pod(
            name=pod_id,
            image=self._docker_image,
            gpu_type=self._settings.gpu_type,
            gpu_count=self._settings.gpu_count,
            stop=self._stop,
            replacements_remaining=self._settings.max_replacements - self._replacements_used,
            start_index=attempt_index,
            provider_sequence=provider_sequence,
            allowed_cuda_versions=allowed_cuda,
        )

        if deployed is None:
            if not self._stop.should_stop:
                logger.warning(f"{pod_id}: failed to get healthy pod")
                await self._discord.notify_pod_deploy_failed(self._hotkey, pod_id)
            return None

        logger.info(f"{pod_id}: pod healthy at {deployed.info.url}")
        return deployed

    async def _cleanup_all(self) -> None:
        """Signal stop and clean up any leftover containers."""
        if not self._stop.should_stop:
            self._stop.cancel("cleanup")
        await self._gpu_manager.cleanup_by_prefix(self._miner_prefix)

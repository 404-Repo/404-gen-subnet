from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

from loguru import logger
from subnet_common.competition.config import CompetitionConfig, require_competition_config
from subnet_common.competition.schedule import require_schedule
from subnet_common.competition.state import RoundStage, require_state
from subnet_common.graceful_shutdown import GracefulShutdown

from .credentials import ACCESS_TOKEN_MARGIN_SECONDS, AccessCredentials
from .models import Job


if TYPE_CHECKING:
    from .generation_iteration import Dependencies
    from .settings import Settings


class Runner(Protocol):
    async def ensure(self, job: Job) -> None: ...
    async def start(self, job: Job, credentials: AccessCredentials, *, min_validity_seconds: int) -> None: ...
    async def terminate(self, job: Job) -> None: ...
    async def status(self, job: Job) -> dict[str, str | None]: ...


class RoundRunner:
    def __init__(
        self,
        dependencies: Dependencies,
        settings: Settings,
        shutdown: GracefulShutdown,
    ) -> None:
        self.dependencies, self.settings, self.shutdown = dependencies, settings, shutdown

    async def active_generation_config(self, job: Job) -> CompetitionConfig | None:
        git = self.dependencies.git
        ref = await git.get_ref_sha(self.settings.github_branch)
        state = await require_state(git, ref)
        if state.current_round != job.snapshot.inputs.number or state.stage != RoundStage.MINER_GENERATION:
            return None
        config = await require_competition_config(git, ref)
        if config.leader_generation_hotkey != job.snapshot.hotkey:
            return None
        schedule = await require_schedule(git, state.current_round, ref)
        if (
            schedule.generation_deadline_block != job.snapshot.inputs.generation_deadline_block
            or schedule.witness_storage_prefix != job.snapshot.inputs.storage_prefix
            or not schedule.witness_save_submissions
        ):
            raise ValueError("Published round schedule differs from the frozen job")
        deadline = schedule.generation_deadline_block
        if deadline is None:
            return config
        block, observed = await self.dependencies.head()
        if not time.time() - 60 <= observed <= time.time() + 5:
            raise ValueError("Generation block is stale")
        return config if block < deadline else None

    async def run(self, job: Job) -> None:
        if self.shutdown.should_stop:
            return
        if job.stopped or job.completed:
            await self.dependencies.runner.terminate(job)
            return
        git = self.dependencies.git
        number = job.snapshot.inputs.number
        ref = await git.get_ref_sha(self.settings.github_branch)
        state = await require_state(git, ref)
        if state.current_round != number:
            await self.dependencies.runner.terminate(job)
            return
        try:
            saved = await self.dependencies.saved.read(job.snapshot)
        except Exception:
            if await self.active_generation_config(job) is None:
                await self.dependencies.runner.terminate(job)
            raise
        expected = set(job.snapshot.inputs.prompt_ids)
        if saved.keys() - expected:
            raise ValueError("Saved result contains an unexpected official prompt ID")
        completed = sum(result.is_terminal() for result in saved.values())
        delta = completed - job.completed_prompts
        logger.info(
            "Round {} saved {}/{} prompts (+{}, {} pending)",
            number,
            completed,
            len(expected),
            delta,
            len(expected) - completed,
        )
        if completed == len(expected):
            job.completed_prompts = completed
            self.dependencies.store.save(job)
            await self.dependencies.runner.terminate(job)
            return
        config = await self.active_generation_config(job)
        if config is None:
            await self.dependencies.runner.terminate(job)
            return

        allocation = job.allocation
        if allocation is not None and allocation.phase == "creating":
            await self.dependencies.runner.status(job)
        if delta < 0:
            raise ValueError("Saved completed prompt count regressed")
        now = time.time()
        progress_at = job.last_progress_at
        if delta or progress_at is None:
            progress_at = now
            job.completed_prompts = completed
            job.last_progress_at = progress_at
            job.progress_warning_sent = False
            self.dependencies.store.save(job)
        if allocation is not None and allocation.phase not in ("intent", "terminated"):
            stalled = now - progress_at
            if stalled >= 1200 and not job.progress_warning_sent:
                job.progress_warning_sent = True
                self.dependencies.store.save(job)
                if self.dependencies.discord is not None:
                    await self.dependencies.discord.send_embed(
                        title="Leader generation stalled",
                        color=0xE67E22,
                        description=(
                            f"Round {job.snapshot.inputs.number}: no new saved prompts "
                            f"for {int(stalled / 60)} minutes."
                        ),
                        fields=[{"name": "Completed", "value": f"{completed}/{len(expected)}", "inline": True}],
                    )
            if stalled < self.settings.stall_replacement_seconds:
                return
            await self.dependencies.runner.terminate(job)

        min_validity_seconds = max(0, config.generation_stage_minutes) * 60 + ACCESS_TOKEN_MARGIN_SECONDS
        credentials = await self.dependencies.credentials.current(min_validity_seconds=min_validity_seconds)
        if credentials.account_id != job.snapshot.account_id:
            raise ValueError("Provider account differs from the frozen job")
        await self.dependencies.runner.ensure(job)
        if not self.shutdown.should_stop and await self.active_generation_config(job) is not None:
            await self.dependencies.runner.start(job, credentials, min_validity_seconds=min_validity_seconds)

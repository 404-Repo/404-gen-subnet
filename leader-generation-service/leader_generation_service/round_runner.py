from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

from loguru import logger
from subnet_common.competition.config import require_competition_config
from subnet_common.competition.schedule import require_schedule
from subnet_common.competition.state import RoundStage, require_state
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.witness.models import Pod

from .credentials import ACCESS_TOKEN_MARGIN_SECONDS, AccessCredentials


if TYPE_CHECKING:
    from .generation_iteration import Dependencies
    from .settings import Settings


class Runner(Protocol):
    async def cleanup(self) -> None: ...
    async def start(
        self, number: int, hotkey: str, credentials: AccessCredentials, *, min_validity_seconds: int
    ) -> Pod | None: ...
    async def terminate(self, pod: Pod) -> None: ...


class RoundRunner:
    def __init__(
        self,
        dependencies: Dependencies,
        settings: Settings,
        shutdown: GracefulShutdown,
    ) -> None:
        self.dependencies, self.settings, self.shutdown = dependencies, settings, shutdown
        self.round_number: int | None = None
        self.pod: Pod | None = None
        self.completed_prompts = 0
        self.last_progress_at = 0.0
        self.progress_warning_sent = False
        self._queue_reported = False

    async def stop(self) -> None:
        if self.pod is not None:
            await self.dependencies.runner.terminate(self.pod)
            self.pod = None

    async def select_round(self, number: int) -> None:
        if self.round_number != number:
            await self.stop()
            self.round_number = number
            self.completed_prompts = 0
            self._queue_reported = False

    async def deadline_reached(self, deadline: int | None) -> bool:
        if deadline is None:
            return False
        block, observed = await self.dependencies.head()
        if not time.time() - 60 <= observed <= time.time() + 5:
            raise ValueError("Generation block is stale")
        return block >= deadline

    async def _can_start(self, number: int, ref: str, deadline: int | None) -> bool:
        git = self.dependencies.git
        current_ref = await git.get_ref_sha(self.settings.github_branch)
        if current_ref != ref:
            state = await require_state(git, current_ref)
            if state.current_round != number or state.stage != RoundStage.MINER_GENERATION:
                return False
            config = await require_competition_config(git, current_ref)
            if config.leader_generation_hotkey != self.dependencies.hotkey:
                return False
            schedule = await require_schedule(git, number, current_ref)
            if not schedule.witness_save_submissions:
                return False
            deadline = schedule.generation_deadline_block
        return not await self.deadline_reached(deadline)

    async def run(
        self,
        number: int,
        completed: int,
        total: int,
        generation_minutes: int,
        ref: str,
        deadline: int | None,
        *,
        failed: int,
    ) -> None:
        if self.shutdown.should_stop:
            return
        discord = self.dependencies.discord
        delta = completed - self.completed_prompts
        logger.info(
            "Round {} saved {}/{} prompts (+{}, {} pending)",
            number,
            completed,
            total,
            delta,
            total - completed,
        )
        if completed == total:
            self.completed_prompts = completed
            if delta > 0 and discord is not None:
                await discord.send_embed(
                    title=f"Round {number} Leader Generation Complete",
                    color=0xE67E22 if failed else 0x2ECC71,
                    description=f"**{completed - failed}** delivered · **{failed}** failed",
                )
            await self.stop()
            return
        if delta < 0:
            raise ValueError("Saved completed prompt count regressed")
        if not self._queue_reported and discord is not None:
            self._queue_reported = True
            await discord.send_embed(
                title=f"Round {number} Leader Generation Queued",
                color=0x3498DB,
                description=f"**{total}** official prompts",
            )
        if delta:
            self.completed_prompts = completed
            self.last_progress_at = time.time()
            self.progress_warning_sent = False
            if discord is not None:
                await discord.send_embed(
                    title=f"Round {number} Leader Generation Progress",
                    color=0x3498DB,
                    description=f"**{completed}/{total}** prompts with terminal results",
                )
        if self.pod is not None:
            stalled = time.time() - self.last_progress_at
            if stalled >= 1200 and not self.progress_warning_sent:
                self.progress_warning_sent = True
                if discord is not None:
                    await discord.send_embed(
                        title="Leader generation stalled",
                        color=0xE67E22,
                        description=f"Round {number}: no new saved prompts for {int(stalled / 60)} minutes.",
                        fields=[{"name": "Completed", "value": f"{completed}/{total}", "inline": True}],
                    )
            if stalled < self.settings.stall_replacement_seconds:
                return
            await self.stop()

        if self.shutdown.should_stop:
            return
        min_validity_seconds = max(0, generation_minutes) * 60 + ACCESS_TOKEN_MARGIN_SECONDS
        logger.info("Round {} checking subscription credentials", number)
        credentials = await self.dependencies.credentials.current(min_validity_seconds=min_validity_seconds)
        if self.shutdown.should_stop:
            return
        if not await self._can_start(number, ref, deadline):
            logger.info("Round {} no longer eligible for a generation pod", number)
            return
        if self.shutdown.should_stop:
            return
        self.pod = await self.dependencies.runner.start(
            number, self.dependencies.hotkey, credentials, min_validity_seconds=min_validity_seconds
        )
        self.last_progress_at = time.time()
        self.progress_warning_sent = False

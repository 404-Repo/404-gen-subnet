from __future__ import annotations

import time
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol
from urllib.parse import unquote, urlsplit

from loguru import logger
from subnet_common.competition.config import require_competition_config
from subnet_common.competition.generations import GenerationsAdapter, generations_path, get_generations
from subnet_common.competition.schedule import require_schedule
from subnet_common.competition.state import RoundStage, require_state
from subnet_common.graceful_shutdown import GracefulShutdown

from .credentials import ACCESS_TOKEN_MARGIN_SECONDS, AccessCredentials
from .models import Job
from .witness import complete, is_terminal


if TYPE_CHECKING:
    from .generation_iteration import Dependencies
    from .settings import Settings


class Runner(Protocol):
    async def ensure(self, job: Job) -> None: ...
    async def start(self, job: Job, credentials: AccessCredentials, *, min_validity_seconds: int) -> None: ...
    async def terminate(self, job: Job) -> None: ...
    async def status(self, job: Job) -> dict[str, str | None]: ...


def prompt_ids(urls: list[str]) -> set[str]:
    ids = {PurePosixPath(unquote(urlsplit(url).path)).stem for url in urls}
    if len(ids) != len(urls) or not ids or "" in ids:
        raise ValueError("Expected distinct official prompt IDs")
    return ids


class RoundRunner:
    def __init__(
        self,
        dependencies: Dependencies,
        settings: Settings,
        shutdown: GracefulShutdown,
    ) -> None:
        self.dependencies, self.settings, self.shutdown = dependencies, settings, shutdown

    async def may_generate(self, job: Job) -> bool:
        git = self.dependencies.git
        ref = await git.get_ref_sha(self.settings.github_branch)
        state = await require_state(git, ref)
        if state.current_round != job.snapshot.inputs.number or state.stage != RoundStage.MINER_GENERATION:
            return False
        schedule = await require_schedule(git, state.current_round, ref)
        if (
            schedule.generation_deadline_block != job.snapshot.inputs.generation_deadline_block
            or schedule.witness_storage_prefix != job.snapshot.inputs.storage_prefix
            or not schedule.witness_save_submissions
        ):
            raise ValueError("Published round schedule differs from the frozen job")
        deadline = schedule.generation_deadline_block
        if deadline is None:
            return True
        block, observed = await self.dependencies.head()
        if not time.time() - 60 <= observed <= time.time() + 5:
            raise ValueError("Generation block is stale")
        return block < deadline

    async def run(self, job: Job) -> None:
        if self.shutdown.should_stop:
            return
        if job.stopped or job.published:
            await self.dependencies.runner.terminate(job)
        if job.published:
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
            if not job.stopped and not await self.may_generate(job):
                await self.dependencies.runner.terminate(job)
            raise
        expected = set(job.snapshot.inputs.prompt_ids.values())
        finished = complete(saved, expected)
        completed = sum(is_terminal(result) for result in saved.values())
        delta = completed - job.completed_prompts
        logger.info(
            "Round {} saved {}/{} prompts (+{}, {} pending)",
            number,
            completed,
            len(expected),
            delta,
            len(expected) - completed,
        )
        if finished:
            try:
                if not complete(await get_generations(git, number, "leader", ref), expected):
                    content = GenerationsAdapter.dump_json(self.dependencies.saved.public(saved), indent=2).decode()
                    await git.commit_files(
                        files={generations_path(number, "leader"): content},
                        message=f"Update generated.json for round {number}, miner leader",
                        branch=self.settings.github_branch,
                        base_sha=ref,
                    )
            except Exception as error:
                logger.warning("Round {} baseline publication failed ({})", number, type(error).__name__)
            else:
                job.published = True
                self.dependencies.store.save(job)
            finally:
                await self.dependencies.runner.terminate(job)
            return
        if job.stopped:
            return
        if not await self.may_generate(job):
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

        if complete(await get_generations(git, number, "leader", ref), expected):
            job.published = True
            self.dependencies.store.save(job)
            return

        generation_minutes = (await require_competition_config(git, ref)).generation_stage_minutes
        min_validity_seconds = max(0, generation_minutes) * 60 + ACCESS_TOKEN_MARGIN_SECONDS
        credentials = await self.dependencies.credentials.current(min_validity_seconds=min_validity_seconds)
        if credentials.account_id != job.snapshot.account_id:
            raise ValueError("Provider account differs from the frozen job")
        await self.dependencies.runner.ensure(job)
        if not self.shutdown.should_stop and await self.may_generate(job):
            await self.dependencies.runner.start(job, credentials, min_validity_seconds=min_validity_seconds)

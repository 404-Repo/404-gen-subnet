import json
import random
from collections import deque
from math import ceil
from pathlib import Path

from loguru import logger
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from subnet_common.competition.audit_requests import (
    AuditRequest,
    AuditRequests,
    get_audit_requests,
    save_audit_requests,
)
from subnet_common.competition.config import require_competition_config
from subnet_common.competition.generations import get_miner_generations
from subnet_common.competition.match_matrix import MatchMatrix, get_match_matrix, save_match_matrix
from subnet_common.competition.output_verifications import (
    get_failed_hotkeys,
    get_output_verifications,
    get_verified_hotkeys,
)
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.source_audit import (
    get_disqualified_hotkeys,
    get_passed_hotkeys,
    get_source_audits,
)
from subnet_common.competition.state import (
    CompetitionState,
    RoundStage,
    update_competition_state,
)
from subnet_common.competition.submissions import require_submissions
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.match_execution import run_match
from judge_service.models import DuelWinner, MatchReport
from judge_service.settings import settings


class MatchResult(BaseModel):
    """
    Result of a match between two miners.

    Positive margin = right wins, negative = left wins.
    """

    left: str = Field(description="Left miner hotkey (defender)")
    right: str = Field(description="Right miner hotkey (challenger)")
    margin: float = Field(description="Positive = right wins, negative = left wins")
    decisive_prompts: list[str] = Field(default_factory=list, description="Stems of prompts that decided the outcome")


class Timeline(BaseModel):
    local_leaders: list[str] = Field(default_factory=list, description="Local (intermediate winners) leader hotkeys")
    pending_miners: deque[str] = Field(default_factory=deque, description="Pending miners (not yet matched)")

    @property
    def finished(self) -> bool:
        return not self.pending_miners

    @property
    def winner(self) -> str:
        return self.local_leaders[-1] if self.local_leaders else "leader"


class MatchRunner:
    """Runs matches for a round, tracks timelines, requests audits."""

    def __init__(
        self,
        git_batcher: GitBatcher,
        state: CompetitionState,
        openai: AsyncOpenAI,
        hotkeys: list[str],
        seed: int,
        prompts: list[str],
        win_margin: float,
        match_matrix: MatchMatrix,
        audit_requests: AuditRequests,
    ) -> None:
        self._git_batcher = git_batcher
        self._state = state
        self._openai = openai
        self._hotkeys = hotkeys
        self._seed = seed
        self._prompts = prompts
        self._win_margin = win_margin

        self._match_matrix = match_matrix
        self._audit_requests = audit_requests
        self._approved_hotkeys: set[str] = set()
        self._rejected_hotkeys: set[str] = set()

        self._timeline: Timeline | None = None

    @property
    def _round_num(self) -> int:
        return int(self._state.current_round)

    @property
    def _git(self) -> GitHubClient:
        return self._git_batcher.git

    @property
    def _ref(self) -> str:
        return str(self._git_batcher.base_sha)

    @classmethod
    async def create(
        cls,
        git_batcher: GitBatcher,
        state: CompetitionState,
        openai: AsyncOpenAI,
    ) -> "MatchRunner":
        """Load round data and create a runner."""

        round_num = state.current_round
        git = git_batcher.git
        base_sha = git_batcher.base_sha

        competition_config = await require_competition_config(git=git, ref=base_sha)
        seed = await require_seed_from_git(git=git, round_num=round_num, ref=base_sha)
        prompts = await require_prompts(git=git, round_num=round_num, ref=base_sha)

        submissions = await require_submissions(git=git, round_num=round_num, ref=base_sha)
        hotkeys = sorted(submissions.keys(), key=lambda hk: submissions[hk].revealed_at_block)
        logger.debug(f"Sorted hotkeys: {hotkeys}")

        match_matrix = await get_match_matrix(git=git, round_num=round_num, ref=base_sha)
        logger.info(f"Existing matches: {len(match_matrix)}")

        audit_requests = await get_audit_requests(git=git, round_num=round_num, ref=base_sha)
        logger.info(f"Existing audit requests: {len(audit_requests)}")

        return cls(
            git_batcher=git_batcher,
            state=state,
            openai=openai,
            hotkeys=hotkeys,
            seed=seed,
            prompts=prompts,
            win_margin=competition_config.win_margin,
            match_matrix=match_matrix,
            audit_requests=audit_requests,
        )

    async def run(self, shutdown: GracefulShutdown) -> None:
        """Main loop: qualification → timelines → finalize."""
        if not self._hotkeys:
            await self._transition_to_next_stage(reason="No submissions found")
            return

        await self._refresh_verification_state()
        self._hotkeys = self._filter_rejected(self._hotkeys)

        if not self._hotkeys:
            await self._transition_to_next_stage(reason="All submissions rejected")
            return

        qualified = await self._run_qualification(shutdown)

        while not shutdown.should_stop:
            if not qualified:
                await self._transition_to_next_stage(reason="No qualified submissions")
                return

            if self._timeline is not None and self._has_verified_winner():
                await self._finalize(winner=self._timeline.winner)
                return

            if self._timeline is None or not self._timeline.finished:
                await self._run_timeline(qualified, shutdown)
            else:
                duel = self._find_exploratory_duel(qualified)
                if duel:
                    await self._ensure_match(*duel, shutdown=shutdown)
                else:
                    logger.info("All duels complete, waiting for verification")
                    await shutdown.wait(timeout=settings.check_state_interval_seconds)

            await self._refresh_verification_state()
            qualified = self._filter_rejected(qualified)

    async def _run_qualification(self, shutdown: GracefulShutdown) -> list[str]:
        """Run all miners against the base leader. Request audit for the first qualified."""
        qualified: list[str] = []
        first_qualified_audited = self._audit_requests.has_any()

        for hotkey in self._hotkeys:
            if shutdown.should_stop:
                break

            if hotkey in self._rejected_hotkeys:
                continue

            margin, decisive_prompts = await self._ensure_match("leader", hotkey, shutdown)

            if margin >= self._win_margin:
                qualified.append(hotkey)

                if not first_qualified_audited:
                    await self._request_audit(hotkey, decisive_prompts)
                    first_qualified_audited = True

            await self._refresh_verification_state()

        logger.info(f"Qualification complete: {len(qualified)}/{len(self._hotkeys)} qualified")
        return qualified

    def _has_verified_winner(self) -> bool:
        if self._timeline is None:
            return False

        local_leaders = set(self._timeline.local_leaders)

        intersection = self._rejected_hotkeys.intersection(local_leaders)
        if intersection:
            logger.info(f"Timeline rejected. Local leaders disqualified: {[intersection]}")
            self._timeline = None
            return False

        if not self._timeline.finished:
            return False

        if self._approved_hotkeys.issuperset(local_leaders):
            logger.info(f"Timeline verified. New leader found: {[self._timeline.winner]}")
            return True

        return False

    async def _run_timeline(self, qualified: list[str], shutdown: GracefulShutdown) -> None:
        if self._timeline is None:
            self._timeline = Timeline(pending_miners=deque(qualified))

        left = self._timeline.winner
        right = self._timeline.pending_miners.popleft()

        if right in self._rejected_hotkeys:
            return

        margin, decisive_prompts = await self._ensure_match(left, right, shutdown)

        if margin >= self._win_margin:
            self._timeline.local_leaders.append(right)
            await self._request_audit(right, decisive_prompts)

    def _find_exploratory_duel(self, qualified: list[str]) -> tuple[str, str] | None:
        """Find a missing duel between qualified miners."""
        for index, left in enumerate(qualified):
            for right in qualified[index + 1 :]:
                if not self._match_matrix.has(left, right):
                    return left, right
        return None

    async def _ensure_match(self, left: str, right: str, shutdown: GracefulShutdown) -> tuple[float, list[str]]:
        """Run match if not in matrix, save a result. Returns (margin, decisive_prompts)."""
        margin = self._match_matrix.get(left=left, right=right)
        if margin is not None:
            return margin, []

        result = await self._run_match_between(left=left, right=right, shutdown=shutdown)
        self._match_matrix.add(left, right, result.margin)
        await save_match_matrix(self._git_batcher, self._round_num, self._match_matrix)
        return result.margin, result.decisive_prompts

    async def _run_match_between(
        self,
        left: str,
        right: str,
        shutdown: GracefulShutdown,
    ) -> MatchResult:
        """Run a match between two miners."""
        left_gens = await get_miner_generations(self._git, left, self._round_num, ref=self._ref)
        right_gens = await get_miner_generations(self._git, right, self._round_num, ref=self._ref)

        if not left_gens and not right_gens:
            logger.warning(f"No generations for {left[:10]} and {right[:10]}")
            return MatchResult(left=left, right=right, margin=0.0)

        if not left_gens:
            logger.warning(f"No generations for left miner {left[:10]}")
            return MatchResult(left=left, right=right, margin=100.0)

        if not right_gens:
            logger.warning(f"No generations for right miner {right[:10]}")
            return MatchResult(left=left, right=right, margin=-100.0)

        report = await run_match(
            openai=self._openai,
            prompts=self._prompts,
            seed=self._seed,
            left_gens=left_gens,
            right_gens=right_gens,
            left=left,
            right=right,
            max_concurrent_duels=settings.max_concurrent_duels,
            overtime_tolerance_ratio=settings.overtime_tolerance_ratio,
            max_generation_time_seconds=settings.max_generation_time_seconds,
            shutdown=shutdown,
        )

        await self._git_batcher.write(
            path=f"rounds/{self._round_num}/{right}/duels_{left[:10]}.json",
            content=report.model_dump_json(indent=2),
            message=f"Match report: {left[:10]} vs {right[:10]}",
        )

        decisive = self._extract_decisive_prompts(report)
        logger.info(f"Match {left[:10]} vs {right[:10]}: margin={report.margin:+.2%}, decisive={len(decisive)}")

        return MatchResult(
            left=left,
            right=right,
            margin=report.margin,
            decisive_prompts=decisive,
        )

    async def _request_audit(self, hotkey: str, decisive_prompts: list[str]) -> None:
        """Request audit for a miner if not already requested."""
        added = self._audit_requests.add(AuditRequest(hotkey=hotkey, critical_prompts=decisive_prompts))
        if added:
            await save_audit_requests(self._git_batcher, self._round_num, self._audit_requests)
            logger.info(f"Audit requested for {hotkey[:10]}, critical_prompts={len(decisive_prompts)}")

    async def _refresh_verification_state(self) -> bool:
        """Refresh approved/rejected sets from git. Returns True if changed."""

        await self._git_batcher.refresh_base_sha()

        verifications = await get_output_verifications(git=self._git, round_num=self._round_num, ref=self._ref)
        verified = get_verified_hotkeys(verifications)
        failed = get_failed_hotkeys(verifications)

        audits = await get_source_audits(git=self._git, round_num=self._round_num, ref=self._ref)
        passed = get_passed_hotkeys(audits)
        disqualified = get_disqualified_hotkeys(audits)

        approved = verified & passed
        rejected = failed | disqualified

        changed: bool = approved != self._approved_hotkeys or rejected != self._rejected_hotkeys

        if changed:
            self._approved_hotkeys = approved
            self._rejected_hotkeys = rejected
            logger.info(
                f"Verification update: "
                f"approved={len(approved)} (verified={len(verified)}, passed={len(passed)}), "
                f"rejected={len(rejected)} (failed={len(failed)}, disqualified={len(disqualified)})"
            )

        return changed

    def _filter_rejected(self, hotkeys: list[str]) -> list[str]:
        """Filter out rejected hotkeys."""
        return [hk for hk in hotkeys if hk not in self._rejected_hotkeys]

    def _extract_decisive_prompts(self, report: MatchReport) -> list[str]:
        """Extract decisive/critical prompts that need verification.

        Logic based on margin thresholds:
        - Margin >= (overtime_tolerance + win_margin): No prompts need verification
          (even with full fault tolerance, the margin remains above a win threshold)
        - Margin <= win_margin: All right-winning prompts are decisive
        - Margin in between: Randomly select win_maprig of total prompts
        """
        full_tolerance_margin = settings.overtime_tolerance_ratio + self._win_margin
        if report.margin >= full_tolerance_margin:
            return []

        right_wins = [Path(d.prompt).stem for d in report.duels if d.winner == DuelWinner.RIGHT]
        count = max(1, ceil(len(report.duels) * self._win_margin))

        if count >= len(right_wins):
            return right_wins

        return random.Random(self._seed).sample(right_wins, count)  # nosec B311 # noqa: S311

    async def _transition_to_next_stage(self, reason: str) -> None:
        """Transition to the next stage."""

        next_stage = RoundStage.PAUSED if settings.pause_on_stage_end else RoundStage.FINALIZING
        logger.info(f"Transitioning to {next_stage.value}: {reason}")
        self._state.stage = next_stage
        await update_competition_state(self._git_batcher, self._state)
        await self._git_batcher.flush()

    async def _finalize(self, winner: str) -> None:
        await self._git_batcher.write(
            path=f"rounds/{self._round_num}/winner.json",
            content=json.dumps({"hotkey": winner}, indent=2),
            message=f"Winner determined {winner[:10]}",
        )

        await self._transition_to_next_stage(reason=f"Winner {winner[:10]}")

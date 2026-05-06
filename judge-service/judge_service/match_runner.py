from collections import deque

from loguru import logger
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from subnet_common.competition import generation_report, source_audit, verification_audit
from subnet_common.competition.audit_requests import (
    AuditRequest,
    AuditRequests,
    get_audit_requests,
    save_audit_requests,
)
from subnet_common.competition.build_info import require_builds
from subnet_common.competition.config import require_competition_config
from subnet_common.competition.generations import GenerationSource, get_generations
from subnet_common.competition.leader import require_leader_state
from subnet_common.competition.match_matrix import MatchMatrix, get_match_matrix, save_match_matrix
from subnet_common.competition.match_report import DuelWinner, save_match_report
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.round_result import RoundResult, save_round_result
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.state import (
    CompetitionState,
    RoundStage,
    update_competition_state,
)
from subnet_common.competition.submissions import require_submissions
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.audit_execution import run_verification_audit
from judge_service.discord import DiscordNotifier
from judge_service.match_execution import run_match
from judge_service.models import MatchOutcome
from judge_service.settings import Settings


class Timeline(BaseModel):
    local_leaders: list[str] = Field(default_factory=list, description="Local (intermediate winners) leader hotkeys")
    pending_miners: deque[str] = Field(default_factory=deque, description="Pending miners (not yet matched)")

    def reset(self, pending_miners: list[str]) -> None:
        self.local_leaders = []
        self.pending_miners = deque(pending_miners)

    @property
    def finished(self) -> bool:
        return not self.pending_miners

    @property
    def winner(self) -> str:
        return self.local_leaders[-1] if self.local_leaders else "leader"

    def is_rejected(self, rejected_hotkeys: set[str]) -> set[str]:
        """Return rejected local leaders, or empty set if none."""
        local_leaders = set(self.local_leaders)
        rejected = rejected_hotkeys.intersection(local_leaders)
        if rejected:
            logger.info(f"Timeline rejected. Local leaders disqualified: {rejected}")
        return rejected

    def has_verified_winner(self, approved_hotkeys: set[str]) -> bool:
        if not self.finished:
            return False

        local_leaders = set(self.local_leaders)

        if approved_hotkeys.issuperset(local_leaders):
            logger.info(f"Timeline verified. New leader found: {self.winner}")
            return True

        return False


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
        settings: Settings,
        discord: DiscordNotifier,
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
        self._reports: dict[str, generation_report.GenerationReport] = {}
        self._verification_audits: dict[str, verification_audit.VerificationAudit] = {}

        self._settings = settings
        self._discord = discord

        self._timeline: Timeline = Timeline()

    @property
    def _round_num(self) -> int:
        return int(self._state.current_round)

    @property
    def _git(self) -> GitHubClient:
        return self._git_batcher.git

    @property
    def _ref(self) -> str:
        return str(self._git_batcher.base_sha)

    def _get_generation_source(self, hotkey: str) -> GenerationSource:
        """Leader uses generated outputs, challengers use submitted outputs."""
        return GenerationSource.GENERATED if hotkey == "leader" else GenerationSource.SUBMITTED

    @classmethod
    async def create(
        cls,
        git_batcher: GitBatcher,
        state: CompetitionState,
        openai: AsyncOpenAI,
        settings: Settings,
        discord: DiscordNotifier,
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
            settings=settings,
            discord=discord,
        )

    async def run(self, shutdown: GracefulShutdown) -> None:
        """Main loop: qualification → timelines → finalize."""
        if not self._hotkeys:
            await self._finalize(winner="leader", reason="No submissions found")
            return

        await self._refresh_verification_state()
        self._hotkeys = self._filter_rejected(self._hotkeys)

        if not self._hotkeys:
            await self._finalize(winner="leader", reason="All submissions rejected")
            return

        qualified = await self._run_qualification(shutdown)
        self._timeline.reset(qualified)

        while not shutdown.should_stop:
            if not qualified:
                await self._finalize(winner="leader", reason="No qualified submissions")
                return

            if rejected_leaders := self._timeline.is_rejected(self._rejected_hotkeys):
                self._timeline.reset(qualified)
                await self._discord.notify_timeline_reset(round_num=self._round_num, rejected_hotkeys=rejected_leaders)

            if self._timeline.has_verified_winner(self._approved_hotkeys):
                await self._finalize(winner=self._timeline.winner)
                return

            # Verification first: any hotkey with a COMPLETED orchestrator report and no
            # verification audit yet gets duelled (submitted vs generated) before we run
            # more miner-vs-miner matches. A pending verification could disqualify the
            # current local leader and reset the timeline, so resolving it early avoids
            # wasted match work.
            pending_audit = await self._find_pending_verification()
            if pending_audit:
                await self._run_verification_audit(pending_audit, shutdown)
                refresh_needed = True
            elif not self._timeline.finished:
                match = await self._run_timeline(qualified, shutdown)
                refresh_needed = not match.from_cache
            else:
                duel = self._find_exploratory_duel(qualified)
                if duel:
                    match = await self._run_exploratory_duel(*duel, shutdown=shutdown)
                    refresh_needed = not match.from_cache
                else:
                    logger.info("All duels complete, waiting for verification")
                    await self._git_batcher.flush()
                    await shutdown.wait(timeout=self._settings.check_state_interval_seconds)
                    refresh_needed = True

            if refresh_needed:
                await self._refresh_verification_state()
                qualified = self._filter_rejected(qualified)

        await self._git_batcher.flush()

    async def _run_qualification(self, shutdown: GracefulShutdown) -> list[str]:
        """Run all miners against the base leader. Request audit for the first qualified."""
        qualified: list[str] = []
        first_qualified_audited = self._audit_requests.has_any()

        for hotkey in self._hotkeys:
            if shutdown.should_stop:
                break

            if hotkey in self._rejected_hotkeys:
                continue

            match = await self._ensure_match("leader", hotkey, shutdown)

            if match.margin >= self._win_margin:
                qualified.append(hotkey)

                if not first_qualified_audited:
                    await self._request_audit(hotkey, match.margin)
                    first_qualified_audited = True

            if not match.from_cache:
                await self._refresh_verification_state()

        logger.info(f"Qualification complete: {len(qualified)}/{len(self._hotkeys)} qualified")
        await self._discord.notify_qualification_complete(
            round_num=self._round_num, qualified=len(qualified), total=len(self._hotkeys)
        )
        return qualified

    async def _run_timeline(self, qualified: list[str], shutdown: GracefulShutdown) -> MatchOutcome:
        """Run the next timeline match. Returns MatchOutcome or None if skipped."""
        left = self._timeline.winner
        right = self._timeline.pending_miners.popleft()

        if right in self._rejected_hotkeys:
            logger.info(f"Timeline match. Rejected due to audit: {right[:10]}")
            return MatchOutcome(left=left, right=right, margin=-100.0)

        logger.info(f"Timeline match. Running: {left[:10]} vs {right[:10]}")
        match = await self._ensure_match(left, right, shutdown)

        if match.margin >= self._win_margin:
            self._timeline.local_leaders.append(right)
            await self._request_audit(right, match.margin)
            await self._discord.notify_new_local_leader(
                round_num=self._round_num, hotkey=right, defeated=left, margin=match.margin
            )

        return match

    def _find_exploratory_duel(self, qualified: list[str]) -> tuple[str, str] | None:
        """Find a missing duel between qualified miners."""
        for index, left in enumerate(qualified):
            for right in qualified[index + 1 :]:
                if not self._match_matrix.has(left, right):
                    return left, right
        return None

    async def _run_exploratory_duel(self, left: str, right: str, shutdown: GracefulShutdown) -> MatchOutcome:
        """Run a duel between two miners. Returns MatchOutcome."""
        logger.info(f"Exploratory duel. Running: {left[:10]} vs {right[:10]}")
        match = await self._ensure_match(left, right, shutdown=shutdown)
        return match

    async def _ensure_match(self, left: str, right: str, shutdown: GracefulShutdown) -> MatchOutcome:
        """Run match if not in matrix, save a result. Returns MatchOutcome with a cached flag."""
        margin = self._match_matrix.get(left=left, right=right)
        if margin is not None:
            return MatchOutcome(left=left, right=right, margin=margin, from_cache=True)

        match = await self._run_match_between(left=left, right=right, shutdown=shutdown)

        if shutdown.should_stop:
            return match

        self._match_matrix.add(left, right, match.margin)
        await save_match_matrix(self._git_batcher, self._round_num, self._match_matrix)
        return match

    async def _run_match_between(
        self,
        left: str,
        right: str,
        shutdown: GracefulShutdown,
    ) -> MatchOutcome:
        """Run a match between two miners."""
        left_gens = await get_generations(
            git=self._git,
            round_num=self._round_num,
            hotkey=left,
            source=self._get_generation_source(left),
            ref=self._ref,
        )
        right_gens = await get_generations(
            git=self._git,
            round_num=self._round_num,
            hotkey=right,
            source=self._get_generation_source(right),
            ref=self._ref,
        )

        # A miner counts as "delivered" only if at least one prompt has a usable JS URL.
        # An entry with js=None (miner failure or collector fetch failure) is not a
        # delivery; treating such miners as winners against miners with no file at all
        # (e.g. a leader whose generations weren't produced this round) lets a miner
        # who delivered nothing usable beat a missing leader. Both must be treated
        # symmetrically as "empty" → draw, so the leader keeps the throne.
        left_delivered = any(g.js is not None for g in left_gens.values())
        right_delivered = any(g.js is not None for g in right_gens.values())

        if not left_delivered and not right_delivered:
            logger.warning(f"No usable generations for {left[:10]} and {right[:10]}")
            return MatchOutcome(left=left, right=right, margin=0.0)

        if not left_delivered:
            logger.warning(f"No usable generations for left miner {left[:10]}")
            return MatchOutcome(left=left, right=right, margin=100.0)

        if not right_delivered:
            logger.warning(f"No usable generations for right miner {right[:10]}")
            return MatchOutcome(left=left, right=right, margin=-100.0)

        report = await run_match(
            openai=self._openai,
            prompts=self._prompts,
            seed=self._seed,
            left_gens=left_gens,
            right_gens=right_gens,
            left=left,
            right=right,
            max_concurrent_vlm_calls=self._settings.max_concurrent_vlm_calls,
            max_concurrent_duels=self._settings.max_concurrent_duels,
            shutdown=shutdown,
        )

        if shutdown.should_stop:
            logger.info(f"Match {left[:10]} vs {right[:10]} interrupted by shutdown, discarding partial result")
            return MatchOutcome(left=left, right=right, margin=0.0)

        # Multi-stage judge sets winner+detail on success, or leaves SKIPPED with detail=None
        # when evaluate_duel raised. SKIPPED with detail set is a clean preview-missing skip.
        failed_duels = sum(1 for d in report.duels if d.winner == DuelWinner.SKIPPED and d.detail is None)
        if failed_duels:
            await self._discord.notify_judge_error(failed_duels=failed_duels, total_duels=len(report.duels))

        await save_match_report(self._git_batcher, self._round_num, report)

        logger.info(f"Match {left[:10]} vs {right[:10]}: margin={report.margin:+.2%}")

        return MatchOutcome(left=left, right=right, margin=report.margin)

    async def _request_audit(self, hotkey: str, margin: float) -> None:
        """Request audit for a miner if not already requested."""
        added = self._audit_requests.add(AuditRequest(hotkey=hotkey))
        if added:
            await save_audit_requests(self._git_batcher, self._round_num, self._audit_requests)
            logger.info(f"Audit requested for {hotkey[:10]}")
            await self._discord.notify_audit_requested(round_num=self._round_num, hotkey=hotkey, margin=margin)

    async def _refresh_verification_state(self) -> bool:
        """Refresh approved/rejected sets from git. Returns True if changed."""

        await self._git_batcher.flush()
        await self._git_batcher.refresh_base_sha()

        # Generation orchestrator's own verdict (it self-rejects on timing / failures).
        # Only COMPLETED reports become candidates for the verification duel below.
        reports = await generation_report.get_generation_reports(
            git=self._git, round_num=self._round_num, ref=self._ref
        )
        completed = generation_report.get_completed_hotkeys(reports)
        rejected_by_orchestrator = generation_report.get_rejected_hotkeys(reports)

        # Our own verdict from the submitted-vs-generated verification duel.
        ver_audits = await verification_audit.get_verification_audits(
            git=self._git, round_num=self._round_num, ref=self._ref
        )
        ver_passed = verification_audit.get_passed_hotkeys(ver_audits)
        ver_failed = verification_audit.get_failed_hotkeys(ver_audits)

        audits = await source_audit.get_source_audits(git=self._git, round_num=self._round_num, ref=self._ref)
        source_passed = source_audit.get_passed_hotkeys(audits)
        source_failed = source_audit.get_failed_hotkeys(audits)

        # Approved = orchestrator COMPLETED ∧ verification PASSED ∧ source PASSED.
        # Rejected = any of the three said no.
        approved = completed & ver_passed & source_passed
        rejected = rejected_by_orchestrator | ver_failed | source_failed

        changed: bool = approved != self._approved_hotkeys or rejected != self._rejected_hotkeys

        if changed:
            self._approved_hotkeys = approved
            self._rejected_hotkeys = rejected
            logger.info(
                f"Verification update: "
                f"approved={len(approved)} "
                f"(completed={len(completed)}, ver_passed={len(ver_passed)}, source_passed={len(source_passed)}), "
                f"rejected={len(rejected)} "
                f"(orch={len(rejected_by_orchestrator)}, ver={len(ver_failed)}, source={len(source_failed)})"
            )

        # Stash for the main loop to consult when picking the next unit of work.
        self._reports = reports
        self._verification_audits = ver_audits

        return changed

    async def _find_pending_verification(self) -> str | None:
        """Find the first hotkey with COMPLETED orchestrator report and no verification audit yet."""
        for hotkey, rpt in self._reports.items():
            if rpt.outcome != generation_report.GenerationReportOutcome.COMPLETED:
                continue
            if hotkey in self._verification_audits:
                continue
            if hotkey in self._rejected_hotkeys:
                continue
            return hotkey
        return None

    async def _run_verification_audit(self, hotkey: str, shutdown: GracefulShutdown) -> None:
        """Run one verification duel: SUBMITTED vs GENERATED. Persists report + audit."""
        logger.info(f"Verification duel for {hotkey[:10]}")
        submitted_gens = await get_generations(
            git=self._git,
            round_num=self._round_num,
            hotkey=hotkey,
            source=GenerationSource.SUBMITTED,
            ref=self._ref,
        )
        generated_gens = await get_generations(
            git=self._git,
            round_num=self._round_num,
            hotkey=hotkey,
            source=GenerationSource.GENERATED,
            ref=self._ref,
        )

        audit = await run_verification_audit(
            openai=self._openai,
            git_batcher=self._git_batcher,
            round_num=self._round_num,
            hotkey=hotkey,
            prompts=self._prompts,
            seed=self._seed,
            submitted_gens=submitted_gens,
            generated_gens=generated_gens,
            max_concurrent_vlm_calls=self._settings.max_concurrent_vlm_calls,
            max_concurrent_duels=self._settings.max_concurrent_duels,
            shutdown=shutdown,
        )

        if shutdown.should_stop:
            logger.info(f"Verification for {hotkey[:10]} interrupted by shutdown")
            return

        # Merge into the existing audits dict and persist.
        self._verification_audits[hotkey] = audit
        await verification_audit.save_verification_audits(self._git_batcher, self._round_num, self._verification_audits)

    def _filter_rejected(self, hotkeys: list[str]) -> list[str]:
        """Filter out rejected hotkeys."""
        return [hk for hk in hotkeys if hk not in self._rejected_hotkeys]

    async def _transition_to_next_stage(self, reason: str) -> None:
        """Transition to the next stage."""

        next_stage = RoundStage.PAUSED if self._settings.pause_on_stage_end else RoundStage.FINALIZING
        logger.info(f"Transitioning to {next_stage.value}: {reason}")
        self._state.stage = next_stage
        await update_competition_state(self._git_batcher, self._state)
        await self._git_batcher.flush()

    async def _resolve_winner(self, winner: str) -> RoundResult:
        if winner != "leader":
            builds = await require_builds(self._git, round_num=self._round_num, ref=self._ref)
            build = builds.get(winner)
            if build is None or build.docker_image is None:
                raise RuntimeError(f"Build not found for {winner[:10]}")
            return RoundResult(
                winner_hotkey=winner,
                repo=build.repo,
                commit=build.commit,
                docker_image=build.docker_image,
            )

        leader_state = await require_leader_state(self._git, ref=self._ref)
        leader = leader_state.get_latest()
        if leader is None:
            raise RuntimeError("Leader state not found")

        return RoundResult(
            winner_hotkey="leader",
            repo=leader.repo,
            commit=leader.commit,
            docker_image=leader.docker,
        )

    async def _finalize(self, winner: str, reason: str | None = None) -> None:
        result = await self._resolve_winner(winner)
        await save_round_result(self._git_batcher, self._round_num, result)
        finalize_reason = reason or f"Winner {result.winner_hotkey[:10]}"
        await self._transition_to_next_stage(reason=finalize_reason)
        await self._discord.notify_round_finalized(
            round_num=self._round_num, winner=result.winner_hotkey, reason=reason
        )

"""Choose a round winner by comparing miners' outputs.

Each miner that takes the lead must pass replay validation and a source audit.
If either check fails, reject that miner and rebuild the tournament using
cached match results.

Run available matches while checks are pending. Finalize the round once
every miner in the surviving chain of winners has passed both checks.
"""

from hashlib import blake2b

from loguru import logger
from openai import AsyncOpenAI
from subnet_common.competition.config import require_competition_config
from subnet_common.competition.generations import get_generations
from subnet_common.competition.leader import require_leader_state
from subnet_common.competition.match_matrix import MatchMatrix, get_match_matrix, save_match_matrix
from subnet_common.competition.match_report import save_match_report
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.round_result import RoundResult, save_round_result
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.source_audit import AuditResult, get_source_audits
from subnet_common.competition.state import (
    CompetitionState,
    RoundStage,
    update_competition_state,
)
from subnet_common.competition.submissions import MinerSubmission, require_submissions
from subnet_common.competition.validation import ValidationResult, get_validation_results
from subnet_common.competition.validation_requests import (
    ValidationRequest,
    ValidationRequests,
    get_validation_requests,
    save_validation_requests,
)
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.discord import DiscordNotifier
from judge_service.match_execution import run_match
from judge_service.models import MatchOutcome
from judge_service.settings import Settings
from judge_service.timeline import Timeline, candidate_rejection_reason, render_timeline


class RoundRunner:
    """Drive qualification, timeline matches, validation requests and finalization."""

    def __init__(
        self,
        git_batcher: GitBatcher,
        state: CompetitionState,
        openai: AsyncOpenAI,
        submissions: dict[str, MinerSubmission],
        seed: int,
        prompts: list[str],
        win_margin: float,
        match_matrix: MatchMatrix,
        validation_requests: ValidationRequests,
        settings: Settings,
        discord: DiscordNotifier,
    ) -> None:
        self._git_batcher = git_batcher
        self._state = state
        self._openai = openai
        self._submissions = submissions
        self._hotkeys = list(submissions)
        self._seed = seed
        self._prompts = prompts
        self._win_margin = win_margin

        self._match_matrix = match_matrix
        self._validation_requests = validation_requests

        # Snapshot of external verdicts, refreshed each `_reload_external_state`.
        self._validation_results: dict[str, ValidationResult] = {}
        self._source_audits: dict[str, AuditResult] = {}

        self._settings = settings
        self._discord = discord

        self._last_state: Timeline | None = None

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
        settings: Settings,
        discord: DiscordNotifier,
    ) -> "RoundRunner":
        """Load round data and create a runner."""

        round_num = state.current_round
        git = git_batcher.git
        base_sha = git_batcher.base_sha

        competition_config = await require_competition_config(git=git, ref=base_sha)
        seed = await require_seed_from_git(git=git, round_num=round_num, ref=base_sha)
        prompts = await require_prompts(git=git, round_num=round_num, ref=base_sha)

        submissions = await require_submissions(git=git, round_num=round_num, ref=base_sha)
        submissions = dict(
            sorted(
                submissions.items(),
                key=lambda item: (
                    item[1].revealed_at_block,
                    item[1].tie_breaker or blake2b(f"{seed}:{item[0]}".encode()).hexdigest(),
                ),
            )
        )
        logger.debug(f"Submission order: {list(submissions)}")

        match_matrix = await get_match_matrix(git=git, round_num=round_num, ref=base_sha)
        logger.info(f"Existing matches: {len(match_matrix)}")

        validation_requests = await get_validation_requests(git=git, round_num=round_num, ref=base_sha)
        logger.info(f"Existing validation requests: {len(validation_requests)}")

        return cls(
            git_batcher=git_batcher,
            state=state,
            openai=openai,
            submissions=submissions,
            seed=seed,
            prompts=prompts,
            win_margin=competition_config.win_margin,
            match_matrix=match_matrix,
            validation_requests=validation_requests,
            settings=settings,
            discord=discord,
        )

    async def run(self, shutdown: GracefulShutdown) -> None:
        """Main loop. Each tick: reload, derive state, push newsletter on change,
        request validation for chain members, finalize OR run next unit of work."""
        if not self._hotkeys:
            await self._finalize(winner="leader", reason="No submissions found")
            return

        await self._reload_external_state()
        await self._run_qualification(shutdown)

        reload_needed = False
        while not shutdown.should_stop:
            if reload_needed:
                await self._reload_external_state()

            state = self._derive_state()
            await self._announce_if_changed(state)

            await self._request_validations(state)

            if state.is_finalized:
                await self._finalize(winner=state.winner)
                return

            if state.next_match is not None:
                defender, challenger = state.next_match
                logger.info(f"Timeline match. Running: {defender[:10]} vs {challenger[:10]}")
                match = await self._ensure_match(defender, challenger, shutdown)
                reload_needed = not match.from_cache
                continue

            if exploratory := state.find_exploratory_duel(self._match_matrix):
                match = await self._run_exploratory_duel(*exploratory, shutdown=shutdown)
                reload_needed = not match.from_cache
                continue

            logger.info("All work complete; awaiting external verification")
            await self._git_batcher.flush()
            await shutdown.wait(timeout=self._settings.max_check_state_interval_seconds)
            reload_needed = True

        await self._git_batcher.flush()

    async def _reload_external_state(self) -> None:
        """Reload validation results and source audits. Read errors propagate to the service loop."""
        await self._git_batcher.flush()
        await self._git_batcher.refresh_base_sha()

        self._validation_results = await get_validation_results(git=self._git, round_num=self._round_num, ref=self._ref)
        audits = await get_source_audits(git=self._git, round_num=self._round_num, ref=self._ref)
        self._source_audits = {a.hotkey: a for a in audits}

    async def _run_qualification(self, shutdown: GracefulShutdown) -> None:
        """Run leader-vs-miner matches to populate the match matrix. Idempotent: cached
        matches are no-ops. Validation requests for chain members happen in the main loop.
        """
        for hotkey in self._hotkeys:
            if shutdown.should_stop:
                break
            if (
                candidate_rejection_reason(self._validation_results.get(hotkey), self._source_audits.get(hotkey))
                is not None
            ):
                continue
            match = await self._ensure_match("leader", hotkey, shutdown)
            if not match.from_cache:
                await self._reload_external_state()
        logger.info("Qualification matches complete")

    def _derive_state(self) -> Timeline:
        return Timeline.derive(
            all_hotkeys=self._hotkeys,
            match_matrix=self._match_matrix,
            win_margin=self._win_margin,
            validation_results=self._validation_results,
            source_audits=self._source_audits,
        )

    async def _announce_if_changed(self, state: Timeline) -> None:
        if state == self._last_state:
            return
        await self._discord.notify_timeline_change(round_num=self._round_num, body=render_timeline(state))
        self._last_state = state

    async def _request_validations(self, state: Timeline) -> None:
        """Request one validation pass per local winner, independent of its defender."""
        for entry in state.entries:
            if entry.status != "won" or entry.margin is None:
                continue
            if not self._validation_requests.add(ValidationRequest(hotkey=entry.hotkey)):
                continue
            await save_validation_requests(self._git_batcher, self._round_num, self._validation_requests)
            logger.info(f"Validation requested for {entry.hotkey[:10]}")
            await self._discord.notify_validation_requested(
                round_num=self._round_num, hotkey=entry.hotkey, defeated=entry.defender, margin=entry.margin
            )

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
            ref=self._ref,
        )
        right_gens = await get_generations(
            git=self._git,
            round_num=self._round_num,
            hotkey=right,
            ref=self._ref,
        )

        # A miner counts as "delivered" only if at least one prompt has a usable JS URL.
        # An entry with js=None (miner failure or collector fetch failure) is not a
        # delivery; treating such miners as winners against miners with no file at all
        # (e.g. a leader whose generations weren't produced this round) lets a miner
        # who delivered nothing usable beat a missing leader. Both must be treated
        # symmetrically as "empty" → draw, so the leader keeps the throne. An uncontested
        # win scores the clean-sweep margin ±1.0 (+ = right wins), which always clears
        # win_margin.
        left_delivered = any(g.js is not None for g in left_gens.values())
        right_delivered = any(g.js is not None for g in right_gens.values())

        if not left_delivered and not right_delivered:
            logger.warning(f"No usable generations for {left[:10]} and {right[:10]}")
            return MatchOutcome(left=left, right=right, margin=0.0)

        if not left_delivered:
            logger.warning(f"No usable generations for left miner {left[:10]}")
            return MatchOutcome(left=left, right=right, margin=1.0)

        if not right_delivered:
            logger.warning(f"No usable generations for right miner {right[:10]}")
            return MatchOutcome(left=left, right=right, margin=-1.0)

        report = await run_match(
            openai=self._openai,
            prompts=self._prompts,
            seed=self._seed,
            left_gens=left_gens,
            right_gens=right_gens,
            left=left,
            right=right,
            max_concurrent_vlm_calls=self._settings.max_concurrent_vlm_calls,
            shutdown=shutdown,
        )

        if shutdown.should_stop:
            logger.info(f"Match {left[:10]} vs {right[:10]} interrupted by shutdown, discarding partial result")
            return MatchOutcome(left=left, right=right, margin=0.0)

        await self._discord.notify_if_judge_error(report)
        await save_match_report(self._git_batcher, self._round_num, report)

        logger.info(f"Match {left[:10]} vs {right[:10]}: margin={report.margin:+.2%}")

        return MatchOutcome(left=left, right=right, margin=report.margin)

    async def _run_exploratory_duel(self, left: str, right: str, shutdown: GracefulShutdown) -> MatchOutcome:
        """Run a duel between two miners. Returns MatchOutcome."""
        logger.info(f"Exploratory duel. Running: {left[:10]} vs {right[:10]}")
        match = await self._ensure_match(left, right, shutdown=shutdown)
        return match

    async def _finalize(self, winner: str, reason: str | None = None) -> None:
        result = await self._resolve_winner(winner)
        await save_round_result(self._git_batcher, self._round_num, result)
        finalize_reason = reason or f"Winner {result.winner_hotkey[:10]}"
        await self._transition_to_next_stage(reason=finalize_reason)
        await self._discord.notify_round_finalized(
            round_num=self._round_num, winner=result.winner_hotkey, reason=finalize_reason
        )

    async def _resolve_winner(self, winner: str) -> RoundResult:
        if winner != "leader":
            submission = self._submissions[winner]
            return RoundResult(winner_hotkey=winner, repo=submission.repo, commit=submission.commit)

        leader_state = await require_leader_state(self._git, ref=self._ref)
        leader = leader_state.get_latest()
        if leader is None:
            raise RuntimeError("Leader state not found")

        return RoundResult(
            winner_hotkey="leader",
            repo=leader.repo,
            commit=leader.commit,
        )

    async def _transition_to_next_stage(self, reason: str) -> None:
        """Transition to the next stage."""

        next_stage = RoundStage.PAUSED if self._settings.pause_on_stage_end else RoundStage.FINALIZING
        logger.info(f"Transitioning to {next_stage.value}: {reason}")
        self._state.stage = next_stage
        await update_competition_state(self._git_batcher, self._state)
        await self._git_batcher.flush()

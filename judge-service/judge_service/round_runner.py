from loguru import logger
from openai import AsyncOpenAI
from subnet_common.competition.audit_matrix import get_audit_matrix, save_audit_matrix
from subnet_common.competition.audit_requests import (
    AuditRequest,
    AuditRequests,
    get_audit_requests,
    save_audit_requests,
)
from subnet_common.competition.build_info import require_builds
from subnet_common.competition.config import require_competition_config
from subnet_common.competition.generation_report import (
    GenerationReport,
    GenerationReportOutcome,
    get_generation_reports,
)
from subnet_common.competition.generations import GenerationsMap, GenerationSource, get_generations
from subnet_common.competition.leader import require_leader_state
from subnet_common.competition.match_matrix import MatchMatrix, get_match_matrix, save_match_matrix
from subnet_common.competition.match_report import DuelWinner, save_match_report
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.round_result import RoundResult, save_round_result
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.source_audit import AuditResult, AuditVerdict, get_source_audits
from subnet_common.competition.state import (
    CompetitionState,
    RoundStage,
    update_competition_state,
)
from subnet_common.competition.submissions import require_submissions
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.audit_execution import (
    produce_generated_vs_defender_audit,
    produce_generated_vs_submitted_audit,
)
from judge_service.discord import DiscordNotifier
from judge_service.match_execution import run_match
from judge_service.models import MatchOutcome
from judge_service.settings import Settings
from judge_service.timeline import (
    Timeline,
    defender_audit_key,
    render_timeline,
    submitted_audit_key,
)


class RoundRunner:
    """Drives a single round end-to-end: qualification, timeline, exploratory duels,
    per-repeat audit duel production (submitted + defender per repeat), verification
    verdict derivation, and finalization."""

    def __init__(
        self,
        git_batcher: GitBatcher,
        state: CompetitionState,
        openai: AsyncOpenAI,
        hotkeys: list[str],
        seed: int,
        prompts: list[str],
        win_margin: float,
        audit_repeats: int,
        match_matrix: MatchMatrix,
        audit_matrix: MatchMatrix,
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
        self._audit_repeats = audit_repeats

        self._match_matrix = match_matrix
        self._audit_matrix = audit_matrix
        self._audit_requests = audit_requests

        # Snapshot of external verdicts, refreshed each `_reload_external_state`.
        self._reports: dict[str, GenerationReport] = {}
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
    ) -> "RoundRunner":
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

        audit_matrix = await get_audit_matrix(git=git, round_num=round_num, ref=base_sha)
        logger.info(f"Existing audit margins: {len(audit_matrix)}")

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
            audit_repeats=competition_config.audit_repeats,
            match_matrix=match_matrix,
            audit_matrix=audit_matrix,
            audit_requests=audit_requests,
            settings=settings,
            discord=discord,
        )

    async def run(self, shutdown: GracefulShutdown) -> None:
        """Main loop. Each tick: reload, derive state, push newsletter on change,
        request audits for chain members, finalize OR run next unit of work."""
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

            for entry in state.entries:
                if entry.status == "won" and entry.margin is not None:
                    await self._request_audit(
                        hotkey=entry.hotkey,
                        defender=entry.defender or "leader",
                        margin=entry.margin,
                    )

            if state.is_finalized:
                await self._finalize(winner=state.winner)
                return

            if hotkey := self._find_pending_verification():
                await self._run_verification_audit(hotkey, shutdown)
                reload_needed = True
                continue

            if state.next_match is not None:
                defender, challenger = state.next_match
                logger.info(f"Timeline match. Running: {defender[:10]} vs {challenger[:10]}")
                match = await self._ensure_match(defender, challenger, shutdown)
                reload_needed = not match.from_cache
                continue

            if duel := state.find_exploratory_duel(self._match_matrix):
                match = await self._run_exploratory_duel(*duel, shutdown=shutdown)
                reload_needed = not match.from_cache
                continue

            logger.info("All work complete; awaiting external verification")
            await self._git_batcher.flush()
            await shutdown.wait(timeout=self._settings.max_check_state_interval_seconds)
            reload_needed = True

        await self._git_batcher.flush()

    def _derive_state(self) -> Timeline:
        return Timeline.derive(
            all_hotkeys=self._hotkeys,
            match_matrix=self._match_matrix,
            win_margin=self._win_margin,
            audit_matrix=self._audit_matrix,
            audit_repeats=self._audit_repeats,
            audit_requests=self._audit_requests,
            generation_reports=self._reports,
            source_audits=self._source_audits,
        )

    def _is_generation_rejected(self, hotkey: str) -> bool:
        report = self._reports.get(hotkey)
        return report is not None and report.outcome == GenerationReportOutcome.REJECTED

    def _is_generation_completed(self, hotkey: str) -> bool:
        report = self._reports.get(hotkey)
        return report is not None and report.outcome == GenerationReportOutcome.COMPLETED

    def _is_source_audit_failed(self, hotkey: str) -> bool:
        audit = self._source_audits.get(hotkey)
        return audit is not None and audit.verdict == AuditVerdict.FAILED

    async def _announce_if_changed(self, state: Timeline) -> None:
        if state == self._last_state:
            return
        await self._discord.notify_timeline_change(round_num=self._round_num, body=render_timeline(state))
        self._last_state = state

    async def _run_qualification(self, shutdown: GracefulShutdown) -> None:
        """Run leader-vs-miner matches to populate the match matrix. Idempotent: cached
        matches are no-ops. Audit requests for chain members happen in the main loop."""
        for hotkey in self._hotkeys:
            if shutdown.should_stop:
                break
            if self._is_generation_rejected(hotkey) or self._is_source_audit_failed(hotkey):
                continue
            match = await self._ensure_match("leader", hotkey, shutdown)
            if not match.from_cache:
                await self._reload_external_state()
        logger.info("Qualification matches complete")

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

    async def _request_audit(self, hotkey: str, defender: str, margin: float) -> None:
        """Insert or refresh the audit request. Persists and notifies on any change."""
        if not self._audit_requests.add(AuditRequest(hotkey=hotkey, latest_defender=defender)):
            return
        await save_audit_requests(self._git_batcher, self._round_num, self._audit_requests)
        logger.info(f"Audit requested for {hotkey[:10]} (defender: {defender[:10]})")
        await self._discord.notify_audit_requested(
            round_num=self._round_num, hotkey=hotkey, defeated=defender, margin=margin
        )

    async def _reload_external_state(self) -> None:
        """Reload generation reports and source audits from git into the local snapshot."""
        await self._git_batcher.flush()
        await self._git_batcher.refresh_base_sha()

        self._reports = await get_generation_reports(git=self._git, round_num=self._round_num, ref=self._ref)
        audits = await get_source_audits(git=self._git, round_num=self._round_num, ref=self._ref)
        self._source_audits = {a.hotkey: a for a in audits}

    def _find_pending_verification(self) -> str | None:
        """Return the next audited hotkey with missing audit margins. Pure scan of
        audit/generation state — source of truth is audit_requests, generation
        reports, audit_matrix, and source_audit. Skips hotkeys already disqualified
        by out-of-band signals (rejected report, failed source audit) or whose
        generation isn't complete yet."""
        for hotkey in self._audit_requests.hotkeys:
            if self._is_generation_rejected(hotkey):
                continue
            if self._is_source_audit_failed(hotkey):
                continue
            if not self._is_generation_completed(hotkey):
                continue
            request = self._audit_requests.get(hotkey)
            if request is None:
                continue
            for r in range(1, self._audit_repeats + 1):
                if self._audit_matrix.get(submitted_audit_key(r), hotkey) is None:
                    return hotkey
                if self._audit_matrix.get(defender_audit_key(request.latest_defender, r), hotkey) is None:
                    return hotkey
        return None

    async def _run_verification_audit(self, hotkey: str, shutdown: GracefulShutdown) -> None:
        """Produce whichever per-repeat audit duel artifacts are missing for `hotkey`.

        For each repeat in 1..audit_repeats, produce the submitted and defender audit
        duel if the corresponding margin is missing from `audit_matrix`. Idempotent at
        the per-(repeat, kind) level — older defender duels (against prior defenders)
        sit on disk as forensic data and are not consulted by the verdict.
        """
        request = self._audit_requests.get(hotkey)
        if request is None:
            logger.debug(f"No audit request / defender for {hotkey[:10]}; nothing to produce")
            return

        defender = request.latest_defender
        defender_source = self._get_generation_source(defender)

        # Defender outputs are shared across all repeats (defender is run once with
        # repeat_index=1 in generation-only mode, or its SUBMITTED file for regular miners).
        submitted_gens: GenerationsMap | None = None
        defender_gens: GenerationsMap | None = None

        for r in range(1, self._audit_repeats + 1):
            if shutdown.should_stop:
                return

            need_submitted = self._audit_matrix.get(submitted_audit_key(r), hotkey) is None
            need_defender = self._audit_matrix.get(defender_audit_key(defender, r), hotkey) is None
            if not need_submitted and not need_defender:
                continue

            generated_gens = await get_generations(
                git=self._git,
                round_num=self._round_num,
                hotkey=hotkey,
                source=GenerationSource.GENERATED,
                ref=self._ref,
                repeat_index=r,
            )

            if need_submitted and not shutdown.should_stop:
                if submitted_gens is None:
                    submitted_gens = await get_generations(
                        git=self._git,
                        round_num=self._round_num,
                        hotkey=hotkey,
                        source=GenerationSource.SUBMITTED,
                        ref=self._ref,
                    )
                if not any(g.js for g in submitted_gens.values()):
                    logger.warning(f"submitted audit r{r} skipped for {hotkey[:10]}: submitted has no usable outputs")
                elif not any(g.js for g in generated_gens.values()):
                    logger.warning(
                        f"submitted audit r{r} skipped for {hotkey[:10]}: generated_{r} has no usable outputs"
                    )
                else:
                    logger.info(f"Producing submitted audit r{r} for {hotkey[:10]}")
                    report = await produce_generated_vs_submitted_audit(
                        openai=self._openai,
                        git_batcher=self._git_batcher,
                        round_num=self._round_num,
                        hotkey=hotkey,
                        prompts=self._prompts,
                        seed=self._seed,
                        submitted_gens=submitted_gens,
                        generated_gens=generated_gens,
                        repeat_index=r,
                        max_concurrent_vlm_calls=self._settings.max_concurrent_vlm_calls,
                        max_concurrent_duels=self._settings.max_concurrent_duels,
                        shutdown=shutdown,
                    )
                    if not shutdown.should_stop:
                        self._audit_matrix.add(submitted_audit_key(r), hotkey, report.margin)
                        await save_audit_matrix(self._git_batcher, self._round_num, self._audit_matrix)

            if need_defender and not shutdown.should_stop:
                if defender_gens is None:
                    defender_gens = await get_generations(
                        git=self._git,
                        round_num=self._round_num,
                        hotkey=defender,
                        source=defender_source,
                        ref=self._ref,
                    )
                if not any(g.js for g in defender_gens.values()):
                    logger.warning(
                        f"defender audit r{r} skipped for {hotkey[:10]} vs {defender[:10]}: "
                        f"defender has no usable {defender_source.value} outputs"
                    )
                elif not any(g.js for g in generated_gens.values()):
                    logger.warning(
                        f"defender audit r{r} skipped for {hotkey[:10]} vs {defender[:10]}: "
                        f"generated_{r} has no usable outputs"
                    )
                else:
                    logger.info(f"Producing defender audit r{r} for {hotkey[:10]} vs {defender[:10]}")
                    report = await produce_generated_vs_defender_audit(
                        openai=self._openai,
                        git_batcher=self._git_batcher,
                        round_num=self._round_num,
                        audited_hotkey=hotkey,
                        defender_hotkey=defender,
                        prompts=self._prompts,
                        seed=self._seed,
                        defender_gens=defender_gens,
                        audited_generated=generated_gens,
                        repeat_index=r,
                        max_concurrent_vlm_calls=self._settings.max_concurrent_vlm_calls,
                        max_concurrent_duels=self._settings.max_concurrent_duels,
                        shutdown=shutdown,
                    )
                    if not shutdown.should_stop:
                        self._audit_matrix.add(defender_audit_key(defender, r), hotkey, report.margin)
                        await save_audit_matrix(self._git_batcher, self._round_num, self._audit_matrix)

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

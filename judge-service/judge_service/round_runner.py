"""Drives a single round to a fixed point: the verified winner.

The judge is a convergence loop, not a script. One tick:

    reload observations -> derive timeline -> publish changes -> do ONE unit of
    work -> repeat

Observations (ground truth only — derived state never feeds back into derivation):

    match matrix        judge's own match results
    audit matrix        judge's own audit duel margins, keyed by the defender they
                        were produced against
    generation reports  orchestrator: per-miner regeneration progress and verdicts
    source audits       source auditor: per-miner source-level verdicts
    submissions / prompts / seed / config   static for the round

Derived state flows OUT only:

    timeline        recomputed from scratch every tick, never stored
    audit requests  tell the orchestrator whom to regenerate and name the defender
                    to audit against; refreshed from the current timeline for every
                    "won" entry each tick, BEFORE the duel scan reads them — that
                    order is what lets a request go stale and heal

Work selection, one unit per tick, in priority order:

    1. the next missing audit duel (per generated repeat of an audited hotkey)
    2. the next timeline match (challenger vs current chain defender)
    3. an exploratory duel (fill the match matrix between non-rejected miners)
    4. idle: flush, wait, reload

Progress argument — the property that makes the loop correct. In every
non-finalized state at least one of these holds:

    (a) a unit of work is due, or
    (b) the round is blocked on a named external event whose producer will
        deliver it: a generation report progressing (orchestrator), or a source
        audit appearing (source auditor).

Anything else is a stall: the round can never finalize.

Audit margins are looked up by the CURRENT defender only; margins produced against
prior defenders stay in the matrix as forensic data.
"""

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
from subnet_common.competition.generations import GenerationSource, get_generations
from subnet_common.competition.leader import require_leader_state
from subnet_common.competition.match_matrix import MatchMatrix, get_match_matrix, save_match_matrix
from subnet_common.competition.match_report import save_match_report
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

from judge_service.discord import DiscordNotifier
from judge_service.match_execution import run_match
from judge_service.models import AuditDuel, MatchOutcome
from judge_service.settings import Settings
from judge_service.timeline import (
    Timeline,
    audit_repeat_verified,
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

            await self._request_audits(state)

            if state.is_finalized:
                await self._finalize(winner=state.winner)
                return

            if duel := self._next_audit_duel():
                await self._run_audit_duel(duel, shutdown)
                reload_needed = True
                continue

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
        """Reload generation reports and source audits from git into the local snapshot."""
        await self._git_batcher.flush()
        await self._git_batcher.refresh_base_sha()

        self._reports = await get_generation_reports(git=self._git, round_num=self._round_num, ref=self._ref)
        audits = await get_source_audits(git=self._git, round_num=self._round_num, ref=self._ref)
        self._source_audits = {a.hotkey: a for a in audits}

    def _is_generation_rejected(self, hotkey: str) -> bool:
        report = self._reports.get(hotkey)
        return report is not None and report.outcome == GenerationReportOutcome.REJECTED

    def _is_source_audit_failed(self, hotkey: str) -> bool:
        audit = self._source_audits.get(hotkey)
        return audit is not None and audit.verdict == AuditVerdict.FAILED

    def _generated_repeats(self, hotkey: str) -> set[int]:
        """Repeats the report says are fully generated, hence auditable. The report's
        per-repeat stats are the sole authority: the orchestrator appends a repeat's
        stats once it is generated (incrementally while PENDING, all of them once
        COMPLETED). A REJECTED (or missing) report offers nothing to audit."""
        report = self._reports.get(hotkey)
        if report is None or report.outcome == GenerationReportOutcome.REJECTED:
            return set()
        return {stats.repeat_index for stats in report.repeats}

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

    def _derive_state(self) -> Timeline:
        return Timeline.derive(
            all_hotkeys=self._hotkeys,
            match_matrix=self._match_matrix,
            win_margin=self._win_margin,
            audit_matrix=self._audit_matrix,
            audit_repeats=self._audit_repeats,
            generation_reports=self._reports,
            source_audits=self._source_audits,
        )

    async def _announce_if_changed(self, state: Timeline) -> None:
        if state == self._last_state:
            return
        await self._discord.notify_timeline_change(round_num=self._round_num, body=render_timeline(state))
        self._last_state = state

    async def _request_audits(self, state: Timeline) -> None:
        """Sync audit requests to the timeline: insert or refresh one request per won
        entry. Persists and notifies per change. Runs before the duel scan each tick,
        which is what lets a stale defender heal (see module docstring)."""
        for entry in state.entries:
            if entry.status != "won" or entry.margin is None:
                continue
            if not self._audit_requests.add(AuditRequest(hotkey=entry.hotkey, latest_defender=entry.defender)):
                continue
            await save_audit_requests(self._git_batcher, self._round_num, self._audit_requests)
            logger.info(f"Audit requested for {entry.hotkey[:10]} (defender: {entry.defender[:10]})")
            await self._discord.notify_audit_requested(
                round_num=self._round_num, hotkey=entry.hotkey, defeated=entry.defender, margin=entry.margin
            )

    def _next_audit_duel(self) -> AuditDuel | None:
        """The next audit duel whose margin is missing from the audit matrix, in
        production order. One duel at a time — the main loop reloads external state
        between duels, so every duel sees the freshest chain. Single owner of the
        audit gating rules; `_run_audit_duel` executes the item, so the scan and
        the producer can never disagree. Pure scan of audit/generation state:
        audit_requests, generation reports, audit_matrix, and source_audit.

        Both duel kinds run as soon as their repeat is generated. The defender comes
        from the audit request, which the main loop refreshes from the current timeline
        before this scan — duels against a defender that later changes or gets rejected
        stay in the matrix as forensic data and are simply re-produced against the new
        defender's keys.

        Hotkeys already disqualified out-of-band (rejected report, failed source audit)
        yield nothing."""
        for request in self._audit_requests:
            hotkey = request.hotkey
            if self._is_generation_rejected(hotkey) or self._is_source_audit_failed(hotkey):
                continue
            defender = request.latest_defender
            for repeat_index in sorted(self._generated_repeats(hotkey)):
                submitted_duel = AuditDuel(
                    hotkey=hotkey, defender=defender, repeat_index=repeat_index, kind="submitted"
                )
                if self._audit_matrix.get(submitted_duel.matrix_key, hotkey) is None:
                    return submitted_duel
                defender_duel = AuditDuel(hotkey=hotkey, defender=defender, repeat_index=repeat_index, kind="defender")
                if self._audit_matrix.get(defender_duel.matrix_key, hotkey) is None:
                    return defender_duel
        return None

    async def _run_audit_duel(self, duel: AuditDuel, shutdown: GracefulShutdown) -> None:
        """Run one audit duel: save its report, record its margin in the audit
        matrix, and send the verdict summary once every margin for the hotkey is
        present. Skipped without recording when either side has no usable outputs."""
        left_hotkey = duel.hotkey if duel.kind == "submitted" else duel.defender
        left_gens = await get_generations(
            git=self._git,
            round_num=self._round_num,
            hotkey=left_hotkey,
            source=self._get_generation_source(left_hotkey),
            ref=self._ref,
        )
        generated_gens = await get_generations(
            git=self._git,
            round_num=self._round_num,
            hotkey=duel.hotkey,
            source=GenerationSource.GENERATED,
            ref=self._ref,
            repeat_index=duel.repeat_index,
        )

        if not any(g.js for g in left_gens.values()):
            logger.warning(f"{duel.log_label} skipped: {duel.kind} has no usable outputs")
            return
        if not any(g.js for g in generated_gens.values()):
            logger.warning(f"{duel.log_label} skipped: generated_{duel.repeat_index} has no usable outputs")
            return

        logger.info(f"Running {duel.log_label}")
        report = await run_match(
            openai=self._openai,
            prompts=self._prompts,
            seed=self._seed,
            left_gens=left_gens,
            right_gens=generated_gens,
            left=duel.left,
            right=duel.hotkey,
            max_concurrent_vlm_calls=self._settings.max_concurrent_vlm_calls,
            shutdown=shutdown,
        )
        # A duel interrupted by shutdown is partial — don't persist it.
        if shutdown.should_stop:
            return
        await self._discord.notify_if_judge_error(report)
        await save_match_report(
            self._git_batcher, self._round_num, report, kind="audit", repeat_index=duel.repeat_index
        )
        logger.info(f"{duel.log_label}: margin={report.margin:+.2%}")
        self._audit_matrix.add(duel.matrix_key, duel.hotkey, report.margin)
        await save_audit_matrix(self._git_batcher, self._round_num, self._audit_matrix)
        await self._notify_audit_verdict(hotkey=duel.hotkey, defender=duel.defender)

    async def _notify_audit_verdict(self, hotkey: str, defender: str) -> None:
        """Send the audit summary once every per-repeat margin is present. Called only
        after new margins were produced, so the notification fires once per hotkey."""
        repeats: list[tuple[float, float]] = []
        for repeat_index in range(1, self._audit_repeats + 1):
            submitted_margin = self._audit_matrix.get(submitted_audit_key(repeat_index), hotkey)
            defender_margin = self._audit_matrix.get(defender_audit_key(defender, repeat_index), hotkey)
            if submitted_margin is None or defender_margin is None:
                return
            repeats.append((submitted_margin, defender_margin))
        verified = all(
            audit_repeat_verified(submitted_margin, defender_margin, self._win_margin)
            for submitted_margin, defender_margin in repeats
        )
        await self._discord.notify_audit_completed(
            round_num=self._round_num,
            hotkey=hotkey,
            defender=defender,
            repeats=repeats,
            verified=verified,
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

    async def _transition_to_next_stage(self, reason: str) -> None:
        """Transition to the next stage."""

        next_stage = RoundStage.PAUSED if self._settings.pause_on_stage_end else RoundStage.FINALIZING
        logger.info(f"Transitioning to {next_stage.value}: {reason}")
        self._state.stage = next_stage
        await update_competition_state(self._git_batcher, self._state)
        await self._git_batcher.flush()

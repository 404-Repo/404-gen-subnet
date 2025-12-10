import asyncio
from pathlib import Path

import httpx
from loguru import logger
from openai import AsyncOpenAI
from subnet_common.competition.config import require_competition_config
from subnet_common.competition.generations import GenerationResult, get_miner_generations
from subnet_common.competition.judge_progress import get_judge_progress, save_judge_progress
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.state import CompetitionState, RoundStage, require_state, update_competition_state
from subnet_common.competition.submissions import require_submissions
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.challenge import evaluate_duel
from judge_service.match_report import DuelReport, DuelWinner, MatchOutcome, MatchReport
from judge_service.settings import settings


WINNER_TO_SCORE: dict[DuelWinner, int] = {
    DuelWinner.MINER: 1,
    DuelWinner.LEADER: -1,
    DuelWinner.DRAW: 0,
    DuelWinner.SKIPPED: 0,
}


def _create_openai_client() -> AsyncOpenAI:
    """Create a configured OpenAI client for judge VLM calls."""
    return AsyncOpenAI(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        http_client=httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)),
    )


async def run_judge_iteration(shutdown: GracefulShutdown) -> None:
    """Run one full judge cycle: evaluate all pending challengers against the current leader.

    Loads competition state from Git, iterates through miners in reveal order,
    runs duels, and persists results. Handles graceful shutdown between matches.
    """
    async with GitHubClient(
        repo=settings.github_repo,
        token=settings.github_token.get_secret_value(),
    ) as git:
        latest_commit_sha = await git.get_ref_sha(ref=settings.github_branch)
        logger.info(f"Latest commit SHA: {latest_commit_sha}")

        state = await require_state(git, ref=latest_commit_sha)
        logger.info(f"Current state: {state}")

        if state.stage != RoundStage.DUELS:
            return

        git_batcher = await GitBatcher.create(git=git, branch=settings.github_branch, base_sha=latest_commit_sha)

        await _run_duels(
            git=git,
            git_batcher=git_batcher,
            state=state,
            ref=latest_commit_sha,
            shutdown=shutdown,
        )

        await git_batcher.flush()


async def _run_duels(
    git: GitHubClient,
    git_batcher: GitBatcher,
    state: CompetitionState,
    ref: str,
    shutdown: GracefulShutdown,
) -> None:
    """Execute duel matches for all pending challengers.

    Iterates through miners sorted by reveal block, challenging the current leader.
    Saves progress after each match for resumability.
    """
    competition_config = await require_competition_config(git, ref=ref)
    submissions = await require_submissions(git, state.current_round, ref=ref)
    seed = await require_seed_from_git(git, state.current_round, ref=ref)
    prompts = await require_prompts(git, state.current_round, ref=ref)

    hotkeys = sorted(submissions.keys(), key=lambda hotkey: submissions[hotkey].revealed_at_block)
    logger.debug(f"Sorted hotkeys: {hotkeys}")

    if len(hotkeys) == 0:
        logger.error(f"`{RoundStage.DUELS.value}` stage with no contenders.")
        return

    progress = await get_judge_progress(git, state.current_round, ref=ref)
    remaining = [hk for hk in hotkeys if hk not in progress.completed_hotkeys]
    logger.info(
        f"Resuming from leader={progress.leader[:10]}, {len(progress.completed_hotkeys)}/{len(hotkeys)} completed"
    )

    openai = _create_openai_client()

    leader = progress.leader
    for hotkey in remaining:
        if shutdown.should_stop:
            break

        prev_leader = leader
        leader = await _challenge_leader(
            openai=openai,
            git_batcher=git_batcher,
            leader=leader,
            hotkey=hotkey,
            state=state,
            prompts=prompts,
            win_margin=competition_config.win_margin,
            seed=seed,
            ref=ref,
            shutdown=shutdown,
        )
        logger.info(f"{prev_leader[:10]} vs {hotkey[:10]}: {leader[:10]} wins")

        progress.leader = leader
        progress.completed_hotkeys.append(hotkey)
        await save_judge_progress(git_batcher, state.current_round, progress)

    if not shutdown.should_stop:
        state.stage = RoundStage.PAUSED if settings.pause_on_stage_end else RoundStage.FINALIZING
        await update_competition_state(git_batcher, state)


async def _challenge_leader(
    openai: AsyncOpenAI,
    git_batcher: GitBatcher,
    leader: str,
    hotkey: str,
    state: CompetitionState,
    prompts: list[str],
    win_margin: float,
    seed: int,
    ref: str,
    shutdown: GracefulShutdown,
) -> str:
    """Challenge the current leader with a miner and return the winner's hotkey.

    Loads generations for both parties, runs the match, persists the report,
    and returns the hotkey of whoever holds the leader position after the match.
    """
    leader_gens = await get_miner_generations(git_batcher.git, leader, state.current_round, ref=ref)
    challenger_gens = await get_miner_generations(git_batcher.git, hotkey, state.current_round, ref=ref)

    if challenger_gens is None:
        logger.info(f"No generations for miner {hotkey}")
        return leader

    if leader_gens is None:
        logger.info(f"No generations for leader {leader}")
        return hotkey

    report = await _run_match(
        openai=openai,
        prompts=prompts,
        seed=seed,
        leader_gens=leader_gens,
        challenger_gens=challenger_gens,
        leader=leader,
        hotkey=hotkey,
        shutdown=shutdown,
    )

    if report.margin >= win_margin:
        report.outcome = MatchOutcome.CHALLENGER_WINS
    else:
        report.outcome = MatchOutcome.LEADER_DEFENDS

    await git_batcher.write(
        path=f"rounds/{state.current_round}/{hotkey}/duels.json",
        content=report.model_dump_json(indent=2),
        message=f"Match report: {leader[:10]} vs {hotkey[:10]}",
    )

    if report.outcome == MatchOutcome.CHALLENGER_WINS:
        logger.info(
            f"New leader {hotkey[:10]} defeats {leader[:10]}: "
            f"{report.score}/{len(report.duels)}, margin={report.margin:.2%}"
        )
        return hotkey
    else:
        logger.info(
            f"Leader {leader[:10]} defends against {hotkey[:10]}: "
            f"{report.score}/{len(report.duels)}, margin={report.margin:.2%}"
        )
        return leader


async def _run_match(
    openai: AsyncOpenAI,
    prompts: list[str],
    seed: int,
    leader_gens: dict[str, GenerationResult],
    challenger_gens: dict[str, GenerationResult],
    leader: str,
    hotkey: str,
    shutdown: GracefulShutdown,
) -> MatchReport:
    """Run all prompt duels for a match between leader and challenger.

    Evaluates each prompt concurrently (limited by max_concurrent_duels),
    aggregates scores, and returns the complete match report.
    """
    sem = asyncio.Semaphore(settings.max_concurrent_duels)

    duels = await asyncio.gather(
        *[
            _evaluate_prompt(
                openai=openai,
                sem=sem,
                prompt=p,
                leader_gen=leader_gens.get(Path(p).stem, GenerationResult()),
                challenger_gen=challenger_gens.get(Path(p).stem, GenerationResult()),
                leader=leader,
                hotkey=hotkey,
                seed=seed,
                shutdown=shutdown,
            )
            for p in prompts
        ]
    )
    duels = sorted(duels, key=lambda d: d.name)

    score = sum(WINNER_TO_SCORE[d.winner] for d in duels)
    return MatchReport(
        leader=leader,
        hotkey=hotkey,
        score=score,
        margin=score / len(duels),
        duels=duels,
    )


async def _evaluate_prompt(
    openai: AsyncOpenAI,
    sem: asyncio.Semaphore,
    prompt: str,
    leader_gen: GenerationResult,
    challenger_gen: GenerationResult,
    leader: str,
    hotkey: str,
    seed: int,
    shutdown: GracefulShutdown,
) -> DuelReport:
    """Evaluate a single prompt duel between leader and challenger.

    Returns early with the appropriate winner if previews are missing.
    Otherwise, calls the judge VLM to determine the winner.
    """
    stem = Path(prompt).stem

    duel = DuelReport(
        name=stem,
        prompt=prompt,
        leader_ply=leader_gen.ply,
        leader_png=leader_gen.png,
        miner_ply=challenger_gen.ply,
        miner_png=challenger_gen.png,
        winner=DuelWinner.SKIPPED,
        issues="Preview is missing",
    )

    if shutdown.should_stop:
        return duel

    if not leader_gen.png and not challenger_gen.png:
        logger.debug(f"No previews for {stem}")
        duel.winner = DuelWinner.DRAW
        return duel

    if not leader_gen.png:
        logger.debug(f"No preview for {stem} from leader {leader[:10]}")
        duel.winner = DuelWinner.MINER
        return duel

    if not challenger_gen.png:
        logger.debug(f"No preview for {stem} from challenger {hotkey[:10]}")
        duel.winner = DuelWinner.LEADER
        return duel

    async with sem:
        if shutdown.should_stop:
            return duel

        start = asyncio.get_running_loop().time()
        result = await evaluate_duel(
            client=openai,
            prompt_url=prompt,
            left_url=leader_gen.png,
            right_url=challenger_gen.png,
            seed=seed,
        )
        elapsed = asyncio.get_running_loop().time() - start
        logger.debug(f"Duel {stem}: {result.outcome:+d} in {elapsed:.1f}s")

        if result.outcome < 0:
            duel.winner = DuelWinner.LEADER
        elif result.outcome > 0:
            duel.winner = DuelWinner.MINER
        else:
            duel.winner = DuelWinner.DRAW
        duel.issues = result.issues

        return duel

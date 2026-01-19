import json
import asyncio

import bittensor as bt
import time
from loguru import logger
import aioboto3
from subnet_common.competition.config import CompetitionConfig, require_competition_config
from subnet_common.competition.schedule import RoundSchedule, require_schedule
from subnet_common.competition.state import CompetitionState, RoundStage, require_state
from subnet_common.github import GitHubClient
from subnet_common.utils import format_duration
from types_aiobotocore_s3 import S3Client
from r2_client import R2Client
from pydantic import BaseModel, Field

from submission_collector.settings import settings
from submission_collector.submission import Submission, parse_commitment
from subnet_common.competition.submissions import MinerSubmission
from subnet_common.competition.prompts import require_prompts
import httpx


class MinerGenerations(BaseModel):
    hotkey: str = Field(..., min_length=10, description="Bittensor hotkey of winner")
    cdn_url: str = Field(..., description="CDN URL of the directory with generated PLY files")
    generations: dict[str, str] = Field(default_factory=dict, description="Dictionary of prompt names and their corresponding R2 CDN URL.")


class CollectionIteration:

    def __init__(self, *, max_concurrent_requests: int, r2_cdn_url: str) -> None:
        self.miner_generations: dict[str, MinerGenerations] = {}
        """Dictionary of miner generations."""
        self.prompts: set[str] = []
        """Set of prompt names that have been picked for the round."""
        self._SECONDS_PER_BLOCK: int = 12
        """Average block time on Bittensor network."""
        self._upload_sem: asyncio.Semaphore = asyncio.Semaphore(max_concurrent_requests)
        """Semaphore to limit the number of concurrent requests to the subnet owner R2 storage for uploading generations."""
        self._download_sem: asyncio.Semaphore = asyncio.Semaphore(max_concurrent_requests)
        """Semaphore to limit the number of concurrent requests to the CDN and R2 for downloading generations."""
        self._GENERATION_EXTENSIONS: list[str] = ["ply", "glb"]
        """List of generation extensions."""
        self._r2_cdn_url: str = r2_cdn_url

    async def run_collection_iteration(self) -> int | None:
        """Run one submission collection iteration.
        Waits for the reveal window to close, collects valid submissions from a chain,
        and commits results to Git.
        Returns seconds to wait before the next iteration, or None for a default interval.
        """
        async with GitHubClient(
            repo=settings.github_repo,
            token=settings.github_token.get_secret_value(),
        ) as git:
            latest_commit_sha = await git.get_ref_sha(ref=settings.github_branch)
            logger.info(f"Latest commit SHA: {latest_commit_sha}")

            state = await require_state(git, ref=latest_commit_sha)
            logger.info(f"Current state: {state}")

            # Load competition config
            config = await require_competition_config(
                git=git,
                ref=latest_commit_sha,
            )
            logger.debug(f"Current competition config: {config}")

            if state.stage != RoundStage.COLLECTING and state.stage != RoundStage.GENERATING:
                return None
            if state.stage == RoundStage.GENERATING:
                if state.generation_deadline is None:
                    # TODO: remove in future. It is here for the last old round.
                    return None
                if time.time() > state.generation_deadline:
                    # If the generation deadline is reached, clear the miner generations and commit the state to duels or finalizing.
                    self.miner_generations.clear()
                    self.prompts.clear()
                    state.stage = RoundStage.DUELS if len(self.miner_generations) > 0 else RoundStage.FINALIZING
                    await self._commit_state(git, latest_commit_sha, state, config)
                    logger.info(f"State committed: {state}")
                    return None

            if not self.miner_generations:
                # Load schedule
                schedule = await require_schedule(
                    git=git,
                    round_num=state.current_round,
                    ref=latest_commit_sha,
                )
                logger.debug(f"Current schedule: {schedule}")

                # Collect submissions
                async with bt.async_subtensor(network=settings.network) as subtensor:
                    wait_seconds = await self._get_wait_seconds(subtensor, schedule)
                    if wait_seconds is not None:
                        return wait_seconds
                    submissions = await self._collect_submissions(subtensor, schedule)
                logger.info(f"Collected submissions: {submissions}")

                # Init miner generations
                # They should be initialized when state is COLLECTING again 
                # or when state is GENERATING but collector was restarted.
                await self._init_miner_generations(state=state, submissions=submissions)

            if state.stage == RoundStage.COLLECTING:
                # If state is collecting move it to generating and set up the generation deadline.
                next_stage = RoundStage.GENERATING if submissions else RoundStage.FINALIZING
                state.stage = RoundStage.PAUSED if settings.pause_on_stage_end else next_stage
                state.generation_deadline = int(time.time()) + settings.generation_duration
                await self._commit_state_and_submissions(
                    git=git,
                    base_sha=latest_commit_sha,
                    state=state,
                    submissions=submissions,
                    config=config,
                )

            if state.stage == RoundStage.GENERATING:
                # If state is generating then collect the generations.
                # Load prompts if they are absent
                if not self.prompts:
                    self.prompts = await require_prompts(git, state.current_round, ref=latest_commit_sha)
                    self.prompts = {prompt.split("/")[-1].split(".")[0] for prompt in self.prompts}

                # Collect generations and update git state.
                await self._collect_generations(round=state.current_round)

                # Commit generations to Git.
                await self._commit_generations(git=git, base_sha=latest_commit_sha, state=state)

            return None  # Wait default interval after a collection


    async def _get_wait_seconds(self, subtensor: bt.async_subtensor, schedule: RoundSchedule) -> int | None:
        """Return seconds to wait until a reveal window closes, or None if ready to collect."""
        current_block = await subtensor.get_current_block()
        target_block = schedule.latest_reveal_block + settings.submission_delay_blocks

        if current_block >= target_block:
            return None

        blocks_left = target_block - current_block
        seconds_left: int = blocks_left * self._SECONDS_PER_BLOCK

        logger.info(
            f"Block {current_block}/{target_block}, " f"{blocks_left} blocks left (~{format_duration(seconds_left)})"
        )

        return seconds_left


    async def _collect_submissions(subtensor: bt.async_subtensor, schedule: RoundSchedule) -> list[Submission]:
        """Fetch and parse valid revealed commitments from a chain."""
        commitments = await subtensor.get_all_revealed_commitments(netuid=settings.netuid)
        logger.debug(f"Revealed commitments: {commitments}")

        submissions = [
            submission
            for hotkey, commitment in commitments.items()
            if (
                submission := parse_commitment(
                    hotkey=hotkey,
                    commitment=commitment,
                    earliest_block=schedule.earliest_reveal_block,
                    latest_block=schedule.latest_reveal_block,
                )
            )
        ]

        submissions.sort(key=lambda s: s.reveal_block)
        logger.success(f"Found {len(submissions)} valid submissions")

        return submissions


    async def _commit_state(
        git: GitHubClient,
        base_sha: str,
        state: CompetitionState,
    ) -> None:
        """Commit state to Git."""
        await git.commit_files(
            files={"state.json": state.model_dump_json(indent=2)},
            message=f"Update state ({state.stage.value})",
            base_sha=base_sha,
            branch=settings.github_branch,
        )

    async def _commit_generations(
        self,
        git: GitHubClient,
        base_sha: str,
        state: CompetitionState,
    ) -> None:
        """Commit generations to Git."""
        files: dict[str, str] = {}
        for hotkey, generations in self.miner_generations.items():
            files[f"rounds/{state.current_round}/{hotkey}/generations.json"] = json.dumps(generations.generations.values(), indent=2)
        await git.commit_files(
            files={"generations.json": json.dumps(self.miner_generations, indent=2)},
            message=f"Update generations for round {state.current_round}",
            base_sha=base_sha,
            branch=settings.github_branch,
        )

    async def _commit_state_and_submissions(
        self,
        git: GitHubClient,
        base_sha: str,
        state: CompetitionState,
        submissions: list[Submission],
        config: CompetitionConfig,
    ) -> None:
        """Commit state and submissions to Git."""
        submissions_data = {
            submission.hotkey: {
                "repo": submission.repo,
                "commit": submission.commit,
                "round": f"{config.name}-{state.current_round}",
                "cdn_url": submission.cdn_url,
                "revealed_at_block": submission.reveal_block,
            }
            for submission in submissions
        }

        await git.commit_files(
            files={
                "state.json": state.model_dump_json(indent=2),
                f"rounds/{state.current_round}/submissions.json": json.dumps(submissions_data, indent=2),
            },
            message=f"Update state ({state.stage.value}) and submissions for round {state.current_round}",
            base_sha=base_sha,
            branch=settings.github_branch,
        )

    async def _init_miner_generations(
        self,
        *,
        state: CompetitionState,
        submissions: list[Submission],
    ) -> None:
        """Initialize miner_generations based on current state and existing data in R2."""
        # For COLLECTING stage we only need basic miner info without generations.
        if state.stage == RoundStage.COLLECTING:
            for submission in submissions:
                self.miner_generations[submission.hotkey] = MinerGenerations(
                    hotkey=submission.hotkey,
                    cdn_url=submission.cdn_url,
                )
            return

        # For GENERATING stage, also inspect R2 to discover already uploaded generations.
        if state.stage == RoundStage.GENERATING:
            async with R2Client(
                access_key_id=settings.r2_access_key_id.get_secret_value(),
                secret_access_key=settings.r2_secret_access_key.get_secret_value(),
                r2_endpoint=settings.r2_endpoint.get_secret_value(),
                bucket=settings.r2_bucket,
            ) as r2_upload_client:
                for submission in submissions:
                    generations: dict[str, str] = {}
                    prefix = f"{state.current_round}/{submission.hotkey}/"
                    try:
                        response = await r2_upload_client.list_objects(prefix=prefix)
                    except Exception as e:
                        logger.error(
                            f"{submission.hotkey}: Failed to list existing generations in R2 for "
                            f"round={state.current_round}: {e}"
                        )
                        continue

                    for key in response.keys:
                        filename = key.split("/")[-1]
                        if "." not in filename:
                            continue
                        prompt_name, extension = filename.rsplit(".", 1)
                        if extension in self._GENERATION_EXTENSIONS:
                            generations[prompt_name] = f"{self._r2_cdn_url}/{key}"

                    self.miner_generations[submission.hotkey] = MinerGenerations(
                        hotkey=submission.hotkey,
                        cdn_url=submission.cdn_url,
                        generations=generations,
                    )

    async def _collect_generations(self, *, round: int) -> None:
        """Collect generations from the Git repository."""
        tasks: list[asyncio.Task] = []
        async with R2Client(
            access_key_id=settings.r2_access_key_id.get_secret_value(),
            secret_access_key=settings.r2_secret_access_key.get_secret_value(),
            r2_endpoint=settings.r2_endpoint.get_secret_value(),
            bucket=settings.r2_bucket,
        ) as r2_upload:
            session = aioboto3.Session()
            for gen in self.miner_generations.values():
                remaining_prompts = [p for p in self.prompts if p not in gen.generations]
                if not remaining_prompts:
                    continue
                logger.info(f"Collecting generations for {gen.hotkey} from {gen.cdn_url} for remaining {len(remaining_prompts)} prompts")
                for prompt in remaining_prompts:
                    task = asyncio.create_task(
                        self._copy_file(
                            cdn_url=gen.cdn_url, 
                            miner_hotkey=gen.hotkey,
                            prompt=prompt,
                            r2_upload=r2_upload,
                            round=round,
                        )
                    )
                    tasks.append(task)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _copy_file(
        self,
        *,
        cdn_url: str, 
        miner_hotkey: str, 
        prompt: str,
        r2_upload: R2Client,
        round: int,
    ) -> None:
        """Copy a file from CDN to R2."""
        # Download from CDN
        async with self._download_sem:
            for extension in self._GENERATION_EXTENSIONS:
                url = f"{cdn_url}/{prompt}.{extension}"
                exists = await self._check_file_exists(url=url)
                if exists:
                    break
            else:
                logger.error(f"{miner_hotkey}: no file found for {prompt} in {cdn_url}")
                return
            data = await self._download_file_chunks(url=url)

        # Upload to R2
        async with self._upload_sem:
            key = f"{round}/{miner_hotkey}/{prompt}.{extension}"
            await r2_upload.upload(key=key, data=data)
            self.miner_generations[miner_hotkey].generations[prompt] = f"{self._r2_cdn_url}/{key}"
            logger.success(f"{miner_hotkey}: downloaded {prompt}.{extension} from {cdn_url} and uploaded to {key}")

    async def _check_file_exists(self, *, url: str) -> bool:
        """Check if a file is available under the given CDN URL."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.head(url, follow_redirects=True)
                return True
            except Exception as e:
                logger.error(f"Failed to check file existence on CDN: {e}")
                return False

    async def _download_file_chunks(self, *, url: str) -> bytes:
        """Download a file from the given URL using chunked streaming."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
        return bytes(data)

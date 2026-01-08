import asyncio
import random
import time

import bittensor as bt
from loguru import logger
from pydantic import BaseModel
from shared.subnet_common.github import GitHubClient
from subnet_common.competition.leader import LeaderState, require_leader_state


class WeightsResult(BaseModel):
    uids: list[int]
    weights: list[float]


class WeightsService:
    def __init__(
        self,
        *,
        subtensor: bt.async_subtensor,
        repo: str,
        branch: str,
        token: str | None,
        set_weights_interval_sec: int,
        set_weights_iteration_timeout_sec: int,
        set_weights_min_retry_interval_sec: int,
        set_weights_max_retry_interval_sec: int,
        next_leader_wait_interval_sec: int,
        subnet_owner_uid: int,
        netuid: int,
        wallet: bt.wallet,
    ) -> None:
        self._subtensor = subtensor
        """Bittensor subtensor instance."""
        self._repo = repo
        """GitHub repo for the competition state."""
        self._branch = branch
        """GitHub branch for the competition state."""
        self._token = token
        """GitHub token for the competition state repo."""
        self._set_weights_interval_sec = set_weights_interval_sec
        """Time interval in seconds to set weights normally."""
        self._set_weights_iteration_timeout_sec = set_weights_iteration_timeout_sec
        """Timeout in seconds for the entire set_weights iteration."""
        self._set_weights_min_retry_interval_sec = set_weights_min_retry_interval_sec
        """Minimum interval in seconds before retrying after failure."""
        self._set_weights_max_retry_interval_sec = set_weights_max_retry_interval_sec
        """Maximum interval in seconds before retrying after failure."""
        self._next_leader_wait_interval_sec = next_leader_wait_interval_sec
        """
        Time interval in seconds to wait for the next leader to become active.
        If next leader is changed soon then set weight just after it becomes active.
        """
        self._subnet_owner_uid = subnet_owner_uid
        """UID of the subnet owner."""
        self._netuid = netuid
        """Subnet UID in bittensor network."""
        self._wallet = wallet
        """Bittensor wallet of the validator."""
        self._set_weights_loop_interval: int = 60  # 1 minute
        """Time interval in seconds for the weights set loop."""
        self._next_set_weights_time: float = 0.0
        """Time when the next weights set should occur."""
        self._BLOCK_TIME = 12  # 12 seconds per block
        """Bittensor block time in seconds."""
        self._confirmation_blocks = 3
        """
        We wait for 3 confirmation blocks to ensure that the weights are updated.
        This is to avoid the case where the weights are set but the network is not yet updated.
        """

    def _schedule_retry(self, current_time: float) -> float:
        """Schedule next weights set with a randomized retry interval."""
        retry_interval = random.uniform(  # nosec B311 # noqa: S311
            self._set_weights_min_retry_interval_sec, self._set_weights_max_retry_interval_sec
        )
        next_time = current_time + retry_interval
        logger.warning(f"Retrying in {retry_interval:.1f}s")
        return next_time

    async def set_weights_loop(self) -> None:
        while True:
            try:
                current_time = time.time()

                if current_time < self._next_set_weights_time:
                    time_until_next = self._next_set_weights_time - current_time
                    logger.trace(f"Next weights set in {time_until_next:.1f}s")
                    continue

                try:
                    success = await asyncio.wait_for(
                        self._set_weights_iteration(), timeout=self._set_weights_iteration_timeout_sec
                    )
                except TimeoutError:
                    logger.error(f"Weights iteration timed out after {self._set_weights_iteration_timeout_sec}s")
                    self._next_set_weights_time = self._schedule_retry(current_time)
                    continue

                if success:
                    self._next_set_weights_time = current_time + self._set_weights_interval_sec
                    logger.info(f"Next weights set scheduled in {self._set_weights_interval_sec}s")
                else:
                    self._next_set_weights_time = self._schedule_retry(current_time)

            except Exception as e:
                logger.exception(f"Unexpected error during weights set iteration: {e}")
                self._next_set_weights_time = self._schedule_retry(time.time())
            finally:
                await asyncio.sleep(self._set_weights_loop_interval)

    async def _set_weights_iteration(self) -> bool:
        """
        Execute one iteration of setting weights.
        Returns: True if weights were successfully set and verified, False otherwise.
        """
        async with self._subtensor as subtensor:
            logger.info("Fetching leader state...")
            leader_state = await self._fetch_leader_state()

            leader_last_block = self._get_leader_last_block(leader_state=leader_state)
            logger.info(f"Leader last block: {leader_last_block}")

            logger.info("Fetching current block...")
            current_block = await subtensor.get_current_block()
            logger.info(f"Current block: {current_block}")

            if leader_last_block is not None and current_block < leader_last_block:
                time_to_block = (leader_last_block - current_block) * self._BLOCK_TIME
                if time_to_block <= self._next_leader_wait_interval_sec:
                    logger.info(
                        f"Postponing weights set, waiting {time_to_block}s for next leader "
                        f"(leader_last_block={leader_last_block}, current_block={current_block})"
                    )
                    return True

            logger.info("Resolving leader...")
            leader_uid, leader_weight = await self._resolve_leader(subtensor, leader_state)
            logger.info(f"Leader: UID={leader_uid}, weight={leader_weight}")

            weights = self._calculate_weights(leader_uid=leader_uid, leader_weight=leader_weight)

            logger.info(f"Setting weights: {weights}")
            res, error_msg = await subtensor.set_weights(
                wallet=self._wallet,
                netuid=self._netuid,
                weights=weights.weights,
                uids=weights.uids,
                wait_for_inclusion=False,
                wait_for_finalization=False,
            )
            if not res:
                logger.warning(f"Failed to set weights: {error_msg}")
                return False

            logger.info("Verifying weights...")
            weights_updated = await self._check_weights_updated(subtensor=subtensor, ref_block=current_block)
            if not weights_updated:
                logger.warning("Weights verification failed")
                return False

            logger.info(f"Successfully set and verified weights: {weights}")
            return True

    async def _fetch_leader_state(self) -> LeaderState:
        async with GitHubClient(repo=self._repo, token=self._token) as git:
            commit_sha = await git.get_ref_sha(ref=self._branch)
            logger.info(f"Commit SHA: {commit_sha}, loading leader state...")
            return await require_leader_state(git, ref=commit_sha)

    def _get_leader_last_block(self, *, leader_state: LeaderState) -> int | None:
        if leader_state is None:
            logger.warning("Leader state not found")
            return None
        effective_block: int = leader_state.get_latest().effective_block
        return effective_block

    async def _resolve_leader(
        self,
        subtensor: bt.async_subtensor,
        leader_state: LeaderState,
    ) -> tuple[int | None, float]:
        logger.info("Getting current block for leader resolution...")
        current_block = await subtensor.get_current_block()
        leader = leader_state.get_leader(current_block)

        if leader is None:
            logger.warning(f"Leader not found for block {current_block}")
            return None, 0.0

        logger.info(f"Fetching metagraph to find UID for leader hotkey: {leader.hotkey}")
        metagraph = await subtensor.metagraph(self._netuid)
        for uid, neuron in enumerate(metagraph.neurons):
            if neuron.hotkey == leader.hotkey:
                return uid, leader.weight

        logger.warning(f"Leader hotkey not found in metagraph: {leader.hotkey}")
        return None, 0.0

    def _calculate_weights(self, *, leader_uid: int | None, leader_weight: float) -> WeightsResult:
        if leader_uid is None or leader_uid == self._subnet_owner_uid:
            return WeightsResult(uids=[self._subnet_owner_uid], weights=[1.0])

        owner_weight = 1.0 - leader_weight
        logger.info(
            f"Calculated weight distribution: leader_uid={leader_uid}, owner_uid={self._subnet_owner_uid}, "
            f"leader_weight={leader_weight}, owner_weight={owner_weight}"
        )
        return WeightsResult(uids=[self._subnet_owner_uid, leader_uid], weights=[owner_weight, leader_weight])

    async def _check_weights_updated(self, *, subtensor: bt.async_subtensor, ref_block: int) -> bool:
        for i in range(self._confirmation_blocks):
            try:
                logger.info(f"Waiting for confirmation block {i+1}/{self._confirmation_blocks}...")
                await subtensor.wait_for_block()
                current_block = await subtensor.get_current_block()
                logger.info(f"Block {current_block} received, fetching metagraph...")
                meta = await subtensor.metagraph(self._netuid)
                validator_uid: int | None = None
                for uid, neuron in enumerate(meta.neurons):
                    if neuron.hotkey == self._wallet.hotkey.ss58_address:
                        validator_uid = uid
                        break

                if validator_uid is None:
                    logger.warning("Validator hotkey not found in metagraph")
                    continue

                last_update = meta.last_update[validator_uid]
                logger.info(
                    f"Verification check: "
                    f"last_update={last_update}, current_block={current_block}, ref_block={ref_block}"
                )
                if last_update >= ref_block:
                    logger.info(f"Weights verification confirmed (last_update={last_update} >= ref_block={ref_block})")
                    return True
                else:
                    logger.warning(f"Weights verification pending (last_update={last_update} < ref_block={ref_block})")
            except Exception as e:
                logger.warning(f"Error during weights verification (attempt {i+1}/{self._confirmation_blocks}): {e}")
        return False

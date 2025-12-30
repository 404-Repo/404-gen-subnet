import asyncio
import time
from typing import cast

import bittensor as bt
from bittensor.core.async_subtensor import get_async_subtensor
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
        subtensor_endpoint: str,
        repo: str,
        branch: str,
        token: str | None,
        set_weights_interval_sec: int,
        set_weights_retry_interval_sec: int,
        next_leader_wait_interval_sec: int,
        subnet_owner_uid: int,
        netuid: int,
        wallet: bt.wallet,
    ) -> None:
        self._subtensor_endpoint = subtensor_endpoint
        """Bittensor subtensor instance."""
        self._subtensor = get_async_subtensor(self._subtensor_endpoint)
        """Bittensor subtensor instance."""
        self._repo = repo
        """GitHub repo for the competition state."""
        self._branch = branch
        """GitHub branch for the competition state."""
        self._token = token
        """GitHub token for the competition state repo."""
        self._set_weights_interval_sec = set_weights_interval_sec
        """Time interval in seconds to set weights normally."""
        self._set_weights_retry_interval_sec = set_weights_retry_interval_sec
        """Time interval in seconds to retry setting weights after an error."""
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
        self._last_successful_set_weights_time: float = 0.0
        """Time of the last successful weights set."""
        self._last_error_set_weights_time: float = 0.0
        """Time of the last error of settings weights."""
        self._BLOCK_TIME = 12  # 12 seconds per block
        """Bittensor block time in seconds."""
        self._confirmation_blocks = 3
        """
        We wait for 3 confirmation blocks to ensure that the weights are updated.
        This is to avoid the case where the weights are set but the network is not yet updated.
        """

    async def set_weights_loop(self) -> None:
        while True:
            try:
                current_time = time.time()
                logger.trace(f"{current_time - self._last_successful_set_weights_time} passed since last set weights.")
                # Weights were set successfully recently
                if current_time - self._last_successful_set_weights_time < self._set_weights_interval_sec:
                    continue
                # Error occurred recently so wait for retry interval
                if current_time - self._last_error_set_weights_time < self._set_weights_retry_interval_sec:
                    continue

                await self._set_weights_iteration()
            except Exception as e:
                logger.exception(f"Error setting weights: {e}")
            finally:
                await asyncio.sleep(self._set_weights_loop_interval)

    async def _set_weights_iteration(self) -> None:
        leader_state = await self._fetch_leader_state()
        logger.info("Leader state retreived")
        leader_last_block = self._get_leader_last_block(leader_state=leader_state)
        logger.info(f"Leader last block retrieved: {leader_last_block}")
        current_block = await self._subtensor.get_current_block()
        logger.info(f"Current block retrieved: {current_block}")
        if leader_last_block is not None and current_block < leader_last_block:
            time_to_block = (leader_last_block - current_block) * self._BLOCK_TIME
            if time_to_block <= self._next_leader_wait_interval_sec:
                logger.info(
                    f"Postponing weights set. "
                    f"Waiting for {time_to_block} seconds for next leader to become active. "
                    f"Leader last block: {leader_last_block}, current block: {current_block}"
                )
                return

        leader_uid, leader_weight = await self._resolve_leader(self._subtensor, leader_state)
        logger.info(f"Leader uid: {leader_uid}, Leader_weight: {leader_weight}")
        weights = self._calculate_weights(leader_uid=leader_uid, leader_weight=leader_weight)
        logger.info("Weights calculated")
        res, error_msg = await self._subtensor.set_weights(
            wallet=self._wallet,
            netuid=self._netuid,
            weights=weights.weights,
            uids=weights.uids,
            wait_for_inclusion=False,
            wait_for_finalization=False,
        )
        if not res:
            logger.warning(f"Weights not updated. Will retry after next interval: {error_msg}")
            self._last_error_set_weights_time = time.time()
            return
        else:
            logger.info("Weights set. Checking...")

        # Wait for confirmation blocks
        weights_updated = await self._check_weights_updated(subtensor=self._subtensor, ref_block=current_block)
        if not weights_updated:
            logger.warning("Weights not updated. Will retry after next interval.")
            self._last_error_set_weights_time = time.time()
            return
        logger.info(f"Weights set successfully: {weights}")
        self._last_successful_set_weights_time = time.time()

    async def _fetch_leader_state(self) -> LeaderState | None:
        async with GitHubClient(repo=self._repo, token=self._token) as git:
            commit_sha = await git.get_ref_sha(ref=self._branch)
            logger.info(f"Latest commit SHA: {commit_sha}")
            return await require_leader_state(git, ref=commit_sha)

    def _get_leader_last_block(self, *, leader_state: LeaderState) -> int | None:
        if leader_state is None:
            logger.warning("No leader state found")
            return None
        return cast(int, leader_state.get_latest().effective_block)

    async def _resolve_leader(
        self,
        subtensor: bt.async_subtensor,
        leader_state: LeaderState | None,
    ) -> tuple[int | None, float]:
        if leader_state is None:
            logger.warning("No leader state found")
            return None, 0.0

        current_block = await subtensor.get_current_block()
        leader = leader_state.get_leader(current_block)

        if leader is None:
            logger.warning(f"No leader for block {current_block}")
            return None, 0.0

        metagraph = await subtensor.metagraph(self._netuid)
        for uid, neuron in enumerate(metagraph.neurons):
            if neuron.hotkey == leader.hotkey:
                return uid, leader.weight

        logger.warning(f"Hotkey {leader.hotkey} not found in metagraph")
        return None, 0.0

    def _calculate_weights(self, *, leader_uid: int | None, leader_weight: float) -> WeightsResult:
        if leader_uid is None or leader_uid == self._subnet_owner_uid:
            return WeightsResult(uids=[self._subnet_owner_uid], weights=[1.0])

        owner_weight = 1.0 - leader_weight
        logger.info(
            f"Leader UID: {leader_uid}, owner UID: {self._subnet_owner_uid}. "
            f"Weights: leader={leader_weight}, owner={owner_weight}"
        )
        return WeightsResult(uids=[self._subnet_owner_uid, leader_uid], weights=[owner_weight, leader_weight])

    async def _check_weights_updated(self, *, subtensor: bt.async_subtensor, ref_block: int) -> bool:
        for i in range(self._confirmation_blocks):
            try:
                await subtensor.wait_for_block()
                current_block = await subtensor.get_current_block()
                logger.info(f"Waited block {i+1}/{self._confirmation_blocks}, current_block={current_block}")
                meta = await subtensor.metagraph(self._netuid)
                validator_uid: int | None = None
                for uid, neuron in enumerate(meta.neurons):
                    if neuron.hotkey == self._wallet.hotkey.ss58_address:
                        validator_uid = uid
                        break
                last_update = meta.last_update[validator_uid]
                logger.info(
                    f"verification: last_update={last_update}, "
                    f"current_block={current_block}, "
                    f"ref_block={ref_block}",
                )
                if last_update >= ref_block:
                    logger.info(f"confirmation OK (last_update={last_update} >= ref={ref_block})")
                    return True
                else:
                    logger.warning(f"confirmation pending (last_update={last_update} < ref={ref_block})")
            except Exception as e:
                logger.warning(f"Error checking weights updated: {i+1}/{self._confirmation_blocks}. Error: {e}")
        return False

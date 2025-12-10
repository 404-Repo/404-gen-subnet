import asyncio

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
        subnet_owner_uid: int,
        netuid: int,
        wallet: bt.wallet,
    ) -> None:
        self._subtensor = subtensor
        self._repo = repo
        self._branch = branch
        self._token = token
        self._set_weights_interval_sec = set_weights_interval_sec
        self._subnet_owner_uid = subnet_owner_uid
        self._netuid = netuid
        self._wallet = wallet

    async def set_weights_loop(self) -> None:
        while True:
            try:
                await self._set_weights_iteration()
            except Exception as e:
                logger.exception(f"Error setting weights: {e}")
            await asyncio.sleep(self._set_weights_interval_sec)

    async def _set_weights_iteration(self) -> None:
        leader_state = await self._fetch_leader_state()

        async with self._subtensor as subtensor:
            leader_uid, leader_weight = await self._resolve_leader(subtensor, leader_state)
            weights = self._calculate_weights(leader_uid=leader_uid, leader_weight=leader_weight)
            await subtensor.set_weights(
                wallet=self._wallet,
                netuid=self._netuid,
                weights=weights.weights,
                uids=weights.uids,
                wait_for_inclusion=False,
                wait_for_finalization=False,
            )
        logger.info(f"Weights set: {weights}")

    async def _fetch_leader_state(self) -> LeaderState | None:
        async with GitHubClient(repo=self._repo, token=self._token) as git:
            commit_sha = await git.get_ref_sha(ref=self._branch)
            logger.info(f"Latest commit SHA: {commit_sha}")
            return await require_leader_state(git, ref=commit_sha)

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

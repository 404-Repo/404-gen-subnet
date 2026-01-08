import asyncio

import bittensor as bt
from loguru import logger

from settings import settings
from weights_service import WeightsService


async def main() -> None:
    logger.info("Starting weights service")
    if settings.wallet_path:
        wallet = bt.wallet(
            name=settings.wallet_name,
            hotkey=settings.wallet_hotkey,
            path=settings.wallet_path,
        )
    else:
        wallet = bt.wallet(
            name=settings.wallet_name,
            hotkey=settings.wallet_hotkey,
        )
    subtensor = bt.async_subtensor(settings.subtensor_endpoint)
    weights_service = WeightsService(
        subtensor=subtensor,
        repo=settings.github_winner_info_repo,
        branch=settings.github_winner_info_branch,
        token=settings.github_token,
        set_weights_interval_sec=settings.set_weights_interval_sec,
        set_weights_iteration_timeout_sec=settings.set_weights_iteration_timeout_sec,
        set_weights_min_retry_interval_sec=settings.set_weights_min_retry_interval_sec,
        set_weights_max_retry_interval_sec=settings.set_weights_max_retry_interval_sec,
        next_leader_wait_interval_sec=settings.next_leader_wait_interval_sec,
        subnet_owner_uid=settings.subnet_owner_uid,
        netuid=settings.netuid,
        wallet=wallet,
    )
    await weights_service.set_weights_loop()


if __name__ == "__main__":
    asyncio.run(main())

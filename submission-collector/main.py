import asyncio
import sys

from loguru import logger
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.utils import format_duration

from submission_collector.collection_iteration import CollectionIteration
from submission_collector.settings import settings


def setup_logging(log_level: str) -> None:
    logger.info("Setting up logging...")
    logger.remove()

    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
        "| <level>{level: <8}</level> "
        "| <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level=log_level,
        colorize=True,
    )


async def main() -> None:
    setup_logging(log_level=settings.log_level)

    logger.info("Submission collector initializing...")

    shutdown = GracefulShutdown()
    shutdown.setup_signal_handlers()

    logger.info("Submission collector started")
    logger.info(f"Network: {settings.network}, NetUID: {settings.netuid}")

    collection_iteration = CollectionIteration(
        max_concurrent_requests=settings.max_concurrent_requests,
        r2_cdn_url=settings.r2_cdn_url,
    )

    while not shutdown.should_stop:
        try:
            wait_seconds = await collection_iteration.run()
            if wait_seconds is None:
                wait_seconds = settings.check_state_interval_seconds
            else:
                wait_seconds = max(60, min(wait_seconds, settings.check_state_interval_seconds))
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"Collection cycle failed: {e}")
            wait_seconds = settings.check_state_interval_seconds
        logger.debug(f"Next cycle in {format_duration(wait_seconds)}")
        await shutdown.wait(timeout=wait_seconds)

    logger.info("Collector stopped gracefully")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)

import asyncio
import sys

from loguru import logger
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.utils import format_duration

from round_manager.manage_round_iteration import run_manage_round_iteration
from round_manager.settings import settings


def setup_logging(log_level: str) -> None:
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

    shutdown = GracefulShutdown()
    shutdown.setup_signal_handlers()

    logger.info("Round manager started")

    while not shutdown.should_stop:
        try:
            await run_manage_round_iteration()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"Round manager cycle failed: {e}")

        logger.debug(f"Next cycle in {format_duration(settings.check_state_interval_seconds)}")
        await shutdown.wait(timeout=settings.check_state_interval_seconds)

    logger.info("Round manager stopped gracefully")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)

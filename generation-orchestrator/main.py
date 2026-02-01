import asyncio
import sys

from loguru import logger
from subnet_common.graceful_shutdown import (
    GracefulShutdown,
)
from subnet_common.utils import calculate_wait_time, format_duration

from generation_orchestrator.generation_iteration import run_generation_iteration
from generation_orchestrator.settings import settings


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

    logger.info("Generation orchestrator started")

    while not shutdown.should_stop:
        next_stage_eta = None
        try:
            next_stage_eta = await run_generation_iteration(shutdown)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"Generation cycle failed with {e}")

        wait_seconds = calculate_wait_time(
            eta=next_stage_eta,
            min_wait_seconds=settings.min_check_state_interval_seconds,
            max_wait_seconds=settings.max_check_state_interval_seconds,
        )
        logger.debug(f"Next cycle in {format_duration(wait_seconds)}")
        await shutdown.wait(timeout=wait_seconds)

    logger.info("Orchestrator stopped gracefully")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)

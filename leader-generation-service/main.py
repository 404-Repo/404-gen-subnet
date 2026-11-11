import asyncio
import sys

from loguru import logger
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.utils import format_duration

from leader_generation_service.generation_iteration import (
    create_dependencies,
    generation_iteration,
    maintain_credentials,
)
from leader_generation_service.round_runner import RoundRunner
from leader_generation_service.server import serve_credentials
from leader_generation_service.settings import Settings
from leader_generation_service.store import exclusive_lock


def setup_logging(log_level: str) -> None:
    logger.remove()
    logger.add(sys.stdout, level=log_level, diagnose=False, backtrace=False)


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level)
    shutdown = GracefulShutdown()
    shutdown.setup_signal_handlers()
    with (
        exclusive_lock(settings.state_directory / ".lock"),
        exclusive_lock(settings.codex_home / "leader-service.lock"),
    ):
        async with create_dependencies(settings, shutdown) as dependencies:
            controller = RoundRunner(dependencies, settings.github_branch, shutdown)
            async with serve_credentials(controller, settings):
                maintenance = asyncio.create_task(maintain_credentials(dependencies, settings, shutdown))
                try:
                    logger.info("Leader generation service started")
                    while not shutdown.should_stop:
                        try:
                            await generation_iteration(controller, settings)
                        except asyncio.CancelledError:
                            break
                        except Exception as error:
                            logger.error("Leader generation cycle failed ({})", type(error).__name__)
                        wait = settings.progress_interval_seconds
                        logger.debug("Next generation iteration in {}", format_duration(wait))
                        await shutdown.wait(timeout=wait)
                finally:
                    shutdown.request_shutdown()
                    await maintenance
                    shutdown.remove_signal_handlers()
    logger.info("Leader generation service stopped gracefully")


if __name__ == "__main__":
    asyncio.run(main())

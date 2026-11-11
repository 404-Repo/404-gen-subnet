import asyncio
import sys
from urllib.parse import urlsplit

from httpx import HTTPStatusError
from loguru import logger
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.utils import format_duration

from leader_generation_service.generation_iteration import (
    create_dependencies,
    generation_iteration,
    maintain_credentials,
)
from leader_generation_service.round_runner import RoundRunner
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
            controller = RoundRunner(dependencies, settings, shutdown)
            maintenance = asyncio.create_task(maintain_credentials(dependencies, settings, shutdown))
            try:
                logger.info(
                    "Leader generation service started. GITHUB_REPO={} GITHUB_BRANCH={} WITNESS_URL={} "
                    "CDN host={} STATE_DIRECTORY={} operator hotkey={} generation image digest={}",
                    settings.github_repo,
                    settings.github_branch,
                    settings.witness_url,
                    urlsplit(settings.cdn_url).hostname,
                    settings.state_directory,
                    dependencies.hotkey,
                    settings.generation_image.split("@", 1)[1],
                )
                while not shutdown.should_stop:
                    wait = settings.progress_interval_seconds
                    retry_in = format_duration(wait)
                    try:
                        await generation_iteration(controller, settings)
                    except asyncio.CancelledError:
                        break
                    except HTTPStatusError as error:
                        logger.error(
                            "Leader generation cycle failed at {} (HTTP {}). Retrying in {}.",
                            error.request.url.host,
                            error.response.status_code,
                            retry_in,
                        )
                    except Exception as error:
                        logger.error(
                            "Leader generation cycle failed ({}). Retrying in {}.", type(error).__name__, retry_in
                        )
                    else:
                        logger.debug("Next generation iteration in {}", retry_in)
                    await shutdown.wait(timeout=wait)
            finally:
                shutdown.request_shutdown()
                await maintenance
                shutdown.remove_signal_handlers()
    logger.info("Leader generation service stopped gracefully")


if __name__ == "__main__":
    asyncio.run(main())

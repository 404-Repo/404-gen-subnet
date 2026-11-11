import asyncio
import sys

from loguru import logger
from subnet_common.discord import DiscordFailureAlert, DiscordWebhook
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.utils import calculate_wait_time, format_duration

from validation_service.discord import ValidationNotifier
from validation_service.settings import Settings
from validation_service.validation_iteration import run_validation_iteration


def setup_logging(log_level: str) -> None:
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
        "| <level>{level: <8}</level> "
        "| <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level=log_level,
        colorize=True,
        diagnose=False,
    )


async def main() -> None:
    settings = Settings()
    setup_logging(log_level=settings.log_level)

    shutdown = GracefulShutdown()
    shutdown.setup_signal_handlers()
    webhook = DiscordWebhook(settings.discord_webhook_url.get_secret_value()) if settings.discord_webhook_url else None
    discord = ValidationNotifier(webhook) if webhook is not None else None
    error_alert = DiscordFailureAlert(
        webhook,
        title="Validation Service Error",
        action="Check the validation service logs for the failed operation.",
    )
    logger.info("Validation service started")

    try:
        while not shutdown.should_stop:
            next_stage_eta = None
            try:
                next_stage_eta = await run_validation_iteration(settings=settings, shutdown=shutdown, discord=discord)
            except asyncio.CancelledError:
                break
            except Exception as error:
                logger.exception(f"Validation cycle failed: {error}")
                await error_alert.notify(error)
            else:
                error_alert.reset()

            wait_seconds = calculate_wait_time(
                eta=next_stage_eta,
                min_wait_seconds=settings.min_check_state_interval_seconds,
                max_wait_seconds=settings.max_check_state_interval_seconds,
            )
            logger.debug(f"Next cycle in {format_duration(wait_seconds)}")
            await shutdown.wait(timeout=wait_seconds)
    finally:
        shutdown.remove_signal_handlers()

    logger.info("Validation service stopped gracefully")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)

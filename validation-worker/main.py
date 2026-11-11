import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import httpx
from loguru import logger
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.r2_client import R2Client

from validation_worker.settings import Settings
from validation_worker.validation import run_validation


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


async def main(round_num: int, hotkey: str, ref: str, output: Path) -> None:
    settings = Settings()
    os.environ["AWS_MAX_ATTEMPTS"] = "1"
    setup_logging(settings.log_level)
    shutdown = GracefulShutdown()
    shutdown.setup_signal_handlers()
    try:
        async with (
            GitHubClient(repo=settings.github_repo, token=settings.github_token.get_secret_value()) as git,
            R2Client(
                access_key_id=settings.r2_access_key_id.get_secret_value(),
                secret_access_key=settings.r2_secret_access_key.get_secret_value(),
                r2_endpoint=settings.r2_endpoint,
                bucket=settings.r2_bucket,
            ) as r2,
            httpx.AsyncClient(timeout=30, trust_env=False) as http,
        ):
            await run_validation(round_num, hotkey, ref, output, git, r2, http, settings, shutdown)
    finally:
        shutdown.remove_signal_handlers()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resume candidate validation on a replay Pod")
    parser.add_argument("--round-num", type=int, required=True)
    parser.add_argument("--hotkey", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-f]{40}", args.ref) is None:
        parser.error("--ref must be a full competition commit SHA")
    asyncio.run(main(round_num=args.round_num, hotkey=args.hotkey, ref=args.ref, output=args.output))

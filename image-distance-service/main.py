import sys

import uvicorn
from loguru import logger

from image_distance_service.settings import settings


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


def main() -> None:
    setup_logging(log_level=settings.log_level)
    logger.info(f"Starting Image Distance Service on {settings.host}:{settings.port}")

    uvicorn.run(
        "image_distance_service.service:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()

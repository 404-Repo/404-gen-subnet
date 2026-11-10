from loguru import logger


async def execute_validation_job(round_num: int, hotkey: str, ref: str) -> None:
    """Log a ready job, leaving validation pending."""
    logger.info(f"Validation job ready: round={round_num}, hotkey={hotkey}, ref={ref}")

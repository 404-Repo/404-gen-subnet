import asyncio

import httpx
from loguru import logger
from tenacity import RetryCallState, retry, retry_if_exception_type, stop_after_attempt


def _log_retry(retry_state: RetryCallState) -> None:
    """Log each retry attempt with the log_id."""
    log_id = retry_state.kwargs.get("log_id", "unknown")
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(f"{log_id}: distance retry {retry_state.attempt_number}/3: {exception}")


@retry(
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError)),
    before_sleep=_log_retry,
    reraise=True,
)
async def _measure_distance_with_retry(
    client: httpx.AsyncClient, endpoint: str, image_url_1: str, image_url_2: str, log_id: str
) -> float | None:
    """Internal distance measurement function with retry logic."""
    start = asyncio.get_running_loop().time()

    response = await client.post(
        f"{endpoint}/distance",
        json={"url_a": image_url_1, "url_b": image_url_2},
    )
    response.raise_for_status()

    result = response.json()
    distance = result.get("distance")

    # Service indicates a file not found or another issue - don't retry
    if distance is None:
        logger.warning(f"{log_id}: distance service reported missing file or error")
        return None

    elapsed = asyncio.get_running_loop().time() - start
    logger.debug(f"{log_id}: distance measured in {elapsed:.1f}s, distance={distance:.4f}")
    return float(distance)


async def measure_distance(
    client: httpx.AsyncClient, endpoint: str, image_url_1: str, image_url_2: str, log_id: str
) -> float | None:
    """
    Measure the distance between two images via external service.

    Args:
        client: httpx async client
        endpoint: Base URL of the distance service
        image_url_1: URL of the first image
        image_url_2: URL of the second image
        log_id: Identifier for logging

    Returns:
        Distance as float on success, None on failure.
        None indicates infrastructure issue - calling code should be treated as identical (no penalty).
        Retries up to 3 times on failure.
    """
    try:
        return await _measure_distance_with_retry(client, endpoint, image_url_1, image_url_2, log_id=log_id)
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError):
        logger.error(f"{log_id}: distance measurement failed after 3 retries")
        return None
    except Exception as e:
        logger.exception(f"{log_id}: distance measurement failed: {e}")
        return None

import asyncio

import httpx
from loguru import logger
from tenacity import RetryCallState, retry, retry_if_exception, stop_after_attempt, wait_exponential


def _is_retryable(exception: BaseException) -> bool:
    """Retry on 5xx, 429 (rate limit), timeouts, and connection errors. Not on other 4xx."""
    if isinstance(exception, httpx.HTTPStatusError):
        status = exception.response.status_code
        return status >= 500 or status == 429
    return isinstance(exception, (httpx.TimeoutException, httpx.RequestError))


def _log_retry(retry_state: RetryCallState) -> None:
    """Log each retry attempt with the log_id."""
    log_id = retry_state.kwargs.get("log_id", "unknown")
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(f"{log_id}: render retry {retry_state.attempt_number}/3: {exception}")


@retry(
    stop=stop_after_attempt(3),
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    before_sleep=_log_retry,
    reraise=True,
)
async def _render_with_retry(client: httpx.AsyncClient, endpoint: str, glb_content: bytes, log_id: str) -> bytes:
    """Internal render function with retry logic."""
    start = asyncio.get_running_loop().time()

    response = await client.post(
        f"{endpoint}/render_glb",
        files={"file": ("content.glb", glb_content, "application/octet-stream")},
    )
    response.raise_for_status()

    elapsed = asyncio.get_running_loop().time() - start
    logger.debug(f"{log_id}: rendered in {elapsed:.1f}s, {len(response.content) / 1024:.1f}KB")
    return response.content


async def render(client: httpx.AsyncClient, endpoint: str, glb_content: bytes, log_id: str) -> bytes | None:
    """
    Render a GLB file to PNG.
    Returns PNG bytes on success, None on failure.
    Retries up to 3 times on 5xx/429/timeout errors.
    """
    try:
        return await _render_with_retry(client, endpoint, glb_content, log_id=log_id)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if _is_retryable(e):
            logger.error(f"{log_id}: render failed after retries (status {status})")
        else:
            logger.warning(f"{log_id}: render rejected (status {status})")
        return None
    except (httpx.TimeoutException, httpx.RequestError):
        logger.error(f"{log_id}: render failed after retries (connection error)")
        return None
    except Exception as e:
        logger.exception(f"{log_id}: render failed: {e}")
        return None

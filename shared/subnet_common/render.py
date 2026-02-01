import asyncio

import httpx
from loguru import logger
from tenacity import RetryCallState, retry, retry_if_exception_type, stop_after_attempt


def _log_retry(retry_state: RetryCallState) -> None:
    """Log each retry attempt with the log_id."""
    log_id = retry_state.kwargs.get("log_id", "unknown")
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(f"{log_id}: render retry {retry_state.attempt_number}/3: {exception}")


@retry(
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError)),
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
    Retries up to 3 times on failure.
    """
    try:
        return await _render_with_retry(client, endpoint, glb_content, log_id=log_id)
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError):
        logger.error(f"{log_id}: render failed after 3 retries")
        return None
    except Exception as e:
        logger.exception(f"{log_id}: render failed: {e}")
        return None

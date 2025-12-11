import asyncio

import httpx
from loguru import logger
from pydantic import BaseModel
from subnet_common.graceful_shutdown import GracefulShutdown

from generation_orchestrator.settings import settings


class GenerationResponse(BaseModel):
    success: bool = False
    content: bytes | None = None
    generation_time: float | None = None


def _is_retryable_status(status: int) -> bool:
    """Check if the HTTP status code is retryable (server errors and rate limiting)."""
    return status >= 500 or status == 429


async def generate(
    request_sem: asyncio.Semaphore,
    endpoint: str,
    image: bytes,
    seed: int,
    log_id: str,
    shutdown: GracefulShutdown,
) -> GenerationResponse:
    if shutdown.should_stop:
        return GenerationResponse(success=False)

    timeout = httpx.Timeout(
        connect=30.0,
        write=30.0,
        read=settings.generation_timeout_seconds + settings.download_timeout_seconds,
        pool=30.0,
    )

    max_attempts = settings.generation_http_attempts

    for attempt in range(max_attempts):
        if shutdown.should_stop:
            return GenerationResponse(success=False)

        if attempt > 0:
            backoff = min(
                settings.generation_http_backoff_base * (2 ** (attempt - 1)),
                settings.generation_http_backoff_max,
            )
            logger.info(f"{log_id}: Retry {attempt + 1}/{max_attempts} after {backoff:.1f}s")
            await asyncio.sleep(backoff)

        result = await _generate_attempt(
            request_sem=request_sem,
            endpoint=endpoint,
            image=image,
            seed=seed,
            log_id=log_id,
            timeout=timeout,
            attempt=attempt,
            max_attempts=max_attempts,
        )

        if result is not None:  # None means retryable failure — continue to next attempt
            return result

    logger.error(f"{log_id}: All {max_attempts} generation attempts failed")
    return GenerationResponse(success=False)


async def _generate_attempt(
    request_sem: asyncio.Semaphore,
    endpoint: str,
    image: bytes,
    seed: int,
    log_id: str,
    timeout: httpx.Timeout,  # noqa: ASYNC109
    attempt: int,
    max_attempts: int,
) -> GenerationResponse | None:
    """Single generation attempt.

    Returns:
        GenerationResponse on success or non-retryable failure.
        None on retryable failure (signals caller to retry).
    """
    sem_released = False
    await request_sem.acquire()

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                start_time = asyncio.get_running_loop().time()

                logger.debug(f"{log_id}: generating (attempt {attempt + 1}/{max_attempts})")

                async with client.stream(
                    "POST",
                    f"{endpoint}/generate",
                    files={"prompt_image_file": ("prompt.jpg", image, "image/jpeg")},
                    data={"seed": seed},
                ) as response:
                    response.raise_for_status()

                    elapsed = asyncio.get_running_loop().time() - start_time

                    request_sem.release()
                    sem_released = True

                    logger.debug(f"{log_id}: generation completed in {elapsed:.1f}s")

                    try:
                        content = await response.aread()
                    except Exception as e:
                        # Failed to read body — could be truncated, worth retrying
                        logger.warning(
                            f"{log_id}: failed to read response body (attempt {attempt + 1}/{max_attempts}): {e}"
                        )
                        return None

                    download_time = asyncio.get_running_loop().time() - start_time - elapsed
                    mb_size = len(content) / 1024 / 1024
                    logger.info(
                        f"Generated for {log_id} in {elapsed:.1f}s, "
                        f"downloaded in {download_time:.1f}s, {mb_size:.1f} MiB"
                    )

                    return GenerationResponse(
                        success=True,
                        content=content,
                        generation_time=elapsed,
                    )

            except httpx.TimeoutException:
                logger.warning(f"{log_id}: generation timed out (attempt {attempt + 1}/{max_attempts})")
                return None  # Retryable

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                try:
                    error_body = await exc.response.aread()
                    error_preview = error_body.decode("utf-8", errors="replace")[:500]
                except Exception:
                    error_preview = "(could not read body)"

                if _is_retryable_status(status):
                    logger.warning(f"{log_id}: HTTP {status} (attempt {attempt + 1}/{max_attempts}): {error_preview}")
                    return None  # Retryable
                else:
                    logger.error(f"{log_id}: HTTP {status} (not retryable): {error_preview}")
                    return GenerationResponse(success=False)

            except httpx.RequestError as e:
                # Connection errors, DNS failures, etc.
                logger.warning(f"{log_id}: request error (attempt {attempt + 1}/{max_attempts}): {e}")
                return None  # Retryable

            except Exception as e:
                logger.error(f"{log_id}: unexpected error during generation: {e}")
                return GenerationResponse(success=False)

    finally:
        if not sem_released:
            request_sem.release()

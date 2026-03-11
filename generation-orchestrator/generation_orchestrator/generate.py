import asyncio
from collections.abc import Callable

import httpx
from loguru import logger
from pydantic import BaseModel


class GenerationResponse(BaseModel):
    success: bool = False
    content: bytes | None = None
    generation_time: float | None = None


async def generate(
    gpu_generation_sem: asyncio.Semaphore,
    endpoint: str,
    image: bytes,
    seed: int,
    log_id: str,
    generation_timeout: float,
    download_timeout: float,
    auth_token: str | None = None,
    is_canceled: Callable[[], bool] | None = None,
) -> GenerationResponse | None:
    """Request a generation and measure time-to-first-bytes.

    We release `gpu_generation_sem` as soon as the first response bytes arrive so the
    next generation can start while the current payload is still downloading.

    is_canceled: Optional check called after acquiring a GPU slot. If returns True,
            skips generation and releases the slot immediately. Useful when another
            worker may have completed the same task while this one was queued.
    """
    timeout = httpx.Timeout(
        connect=30.0,
        write=30.0,
        read=generation_timeout + download_timeout,
        pool=30.0,
    )

    sem_released = False
    # This semaphore represents a GPU "generation slot": we hold it until the
    # first response bytes arrive (generation complete), then release it while
    # the payload is still downloading to maximize GPU utilization.
    await gpu_generation_sem.acquire()

    if is_canceled and is_canceled():
        gpu_generation_sem.release()
        logger.debug(f"{log_id}: cancelled while waiting for GPU slot")
        return None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                start_time = asyncio.get_running_loop().time()

                logger.debug(f"{log_id}: generating")

                headers = {}
                if auth_token:
                    headers["Authorization"] = f"Bearer {auth_token}"

                async with client.stream(
                    "POST",
                    f"{endpoint}/generate",
                    files={"prompt_image_file": ("prompt.jpg", image, "image/jpeg")},
                    data={"seed": seed},
                    headers=headers,
                ) as response:
                    response.raise_for_status()

                    elapsed = asyncio.get_running_loop().time() - start_time

                    # Release the GPU slot as soon as generation finishes (first bytes).
                    gpu_generation_sem.release()
                    sem_released = True

                    logger.debug(f"{log_id}: generation completed in {elapsed:.1f}s")

                    try:
                        content = await response.aread()
                    except Exception as e:
                        logger.warning(f"{log_id}: failed to read response body: {e}")
                        return GenerationResponse(success=False)

                    download_time = asyncio.get_running_loop().time() - start_time - elapsed
                    mb_size = len(content) / 1024 / 1024
                    logger.debug(
                        f"Generated for {log_id} in {elapsed:.1f}s, "
                        f"downloaded in {download_time:.1f}s, {mb_size:.1f} MiB"
                    )

                    return GenerationResponse(
                        success=True,
                        content=content,
                        generation_time=elapsed,
                    )

            except httpx.TimeoutException:
                logger.warning(f"{log_id}: generation timed out")
                return GenerationResponse(success=False)

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                try:
                    error_body = await exc.response.aread()
                    error_preview = error_body.decode("utf-8", errors="replace")[:500]
                except Exception:
                    error_preview = "(could not read body)"

                logger.error(f"{log_id}: HTTP {status}: {error_preview}")
                return GenerationResponse(success=False)

            except httpx.RequestError as e:
                # Connection errors, DNS failures, etc.
                logger.warning(f"{log_id}: request error: {e}")
                return GenerationResponse(success=False)

            except Exception as e:
                logger.error(f"{log_id}: unexpected error during generation: {e}")
                return GenerationResponse(success=False)

    finally:
        if not sem_released:
            gpu_generation_sem.release()

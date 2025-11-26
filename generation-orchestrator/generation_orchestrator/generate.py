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

    sem_released = False
    await request_sem.acquire()

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                start_time = asyncio.get_running_loop().time()
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

                    try:
                        content = await response.aread()
                    except Exception as e:
                        logger.exception(f"Failed to read response body for {log_id}: {e}")
                        return GenerationResponse(success=False)

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
                logger.error(f"Generation timed out for {log_id}")
                return GenerationResponse(success=False)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                try:
                    error_body = await exc.response.aread()
                    error_preview = error_body.decode("utf-8", errors="replace")[:500]
                    logger.error(f"Generation failed for {log_id} with HTTP {status}: {error_preview}")
                except Exception:
                    logger.error(f"Generation failed for {log_id} with HTTP {status} (could not read body)")
                return GenerationResponse(success=False)
            except Exception as e:
                logger.error(f"Unexpected error during generation for {log_id}: {e}")
                return GenerationResponse(success=False)
    finally:
        if not sem_released:
            request_sem.release()

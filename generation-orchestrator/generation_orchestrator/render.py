import asyncio

import httpx
from loguru import logger


async def render(endpoint: str, ply_content: bytes, log_id: str) -> bytes | None:
    """
    Render a PLY file to PNG.
    Returns PNG bytes on success, None on failure.
    """
    try:
        start = asyncio.get_running_loop().time()

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=60.0,
                read=120.0,
                write=120.0,
                pool=60.0,
            )
        ) as client:
            response = await client.post(
                f"{endpoint}/render",
                files={"file": ("content.ply", ply_content, "application/octet-stream")},
            )

        elapsed = asyncio.get_running_loop().time() - start

        if response.status_code != 200:
            logger.error(f"{log_id}: render failed: HTTP {response.status_code} ({elapsed:.1f}s)")
            return None

        logger.info(f"{log_id}: rendered in {elapsed:.1f}s, {len(response.content) / 1024:.1f}KB")
        return response.content

    except httpx.TimeoutException:
        logger.error(f"{log_id}: render timeout")
        return None
    except Exception as e:
        logger.exception(f"{log_id}: render failed: {e}")
        return None

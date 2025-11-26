import asyncio
from pathlib import Path

import aiofiles
import aiofiles.os
import httpx
from loguru import logger
from pydantic import BaseModel
from subnet_common.competition.prompts import require_prompts
from subnet_common.github import GitHubClient


class Prompt(BaseModel):
    name: str
    url: str
    path: Path


class PromptsNotFoundError(Exception):
    pass


async def ensure_prompts_cached_from_git(
    git: GitHubClient,
    round_num: int,
    cache_dir: Path,
    *,
    ref: str = "main",
    max_concurrent: int = 10,
) -> list[Prompt]:
    """Download prompts from a git-tracked TSV file."""
    urls = await require_prompts(git, round_num, ref=ref)
    return await ensure_prompts_cached(urls, get_prompts_cache_dir(cache_dir), max_concurrent=max_concurrent)


def get_prompts_cache_dir(cache_dir: Path) -> Path:
    return cache_dir / "prompts"


async def ensure_prompts_cached(
    urls: list[str],
    cache_dir: Path,
    *,
    max_concurrent: int = 20,
) -> list[Prompt]:
    """Download missing prompts to cache. Returns only successfully cached prompts."""
    cache_dir.mkdir(parents=True, exist_ok=True)

    existing = {f.name for f in cache_dir.iterdir()}
    logger.debug(f"Found {len(existing)} existing prompts in cache")

    missing = [(Path(url).name, url) for url in urls if Path(url).name not in existing]

    if missing:
        logger.info(f"Downloading {len(missing)}/{len(urls)} missing prompts")

        semaphore = asyncio.Semaphore(max_concurrent)

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            tasks = [download_file(client, semaphore, url, cache_dir / filename) for filename, url in missing]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        failed = 0
        for (filename, _), result in zip(missing, results, strict=True):
            if isinstance(result, Exception):
                logger.error(f"Failed to download {filename}: {result}")
                failed += 1

        logger.info(f"Downloaded {len(missing) - failed}/{len(missing)} prompts")
    else:
        logger.info(f"All {len(urls)} prompts already cached")

    # Only return prompts that exist on disk
    existing = {f.name for f in cache_dir.iterdir()}
    return [
        Prompt(name=Path(url).stem, url=url, path=cache_dir / Path(url).name)
        for url in urls
        if Path(url).name in existing
    ]


async def download_file(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
    dest: Path,
) -> None:
    """Download file with streaming. Uses a temp file to avoid partial downloads."""
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")

    async with semaphore:
        logger.debug(f"Downloading {url} to {dest}")
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async with aiofiles.open(tmp_dest, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        await f.write(chunk)

            await aiofiles.os.rename(tmp_dest, dest)

        except BaseException:
            try:
                await aiofiles.os.remove(tmp_dest)
            except OSError:
                pass
            raise

"""Run a generation function over pending prompts and submit results to the Pod."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import cast

import aiofiles
import httpx
from bittensor_wallet import Wallet
from loguru import logger
from witness_client import AsyncWitnessClient, WitnessConfig


MAX_PROMPT_DOWNLOAD_BYTES = 16 * 1024 * 1024


async def read_image(source: str | bytes, http: httpx.AsyncClient, *, max_bytes: int | None = None) -> bytes:
    if isinstance(source, bytes):
        return source
    if source.lower().startswith(("https://", "http://")):
        async with http.stream("GET", source) as response:
            response.raise_for_status()
            if max_bytes is None:
                return await response.aread()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > max_bytes:
                    raise ValueError("Prompt image exceeds its download size limit")
                body.extend(chunk)
            return bytes(body)
    async with aiofiles.open(Path(source).expanduser(), "rb") as image_file:
        return await image_file.read()


async def run_pass(
    generate: Callable[[bytes, int, AsyncWitnessClient], Awaitable[str | None]],
    pending: dict[str, str | bytes],
    seed: int,
    *,
    client: AsyncWitnessClient,
    read_saved: Callable[[httpx.AsyncClient], Awaitable[set[str]]],
    workers: int = 8,
    max_prompt_bytes: int | None = None,
) -> None:
    """Process pending prompts once, removing confirmed submissions as they finish."""
    if not pending:
        return
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as http:
        submitted = await read_saved(http)
        queue = asyncio.Queue[tuple[str, str | bytes]]()
        for prompt_id, source in pending.copy().items():
            if prompt_id in submitted:
                logger.info("{} | already submitted", prompt_id[:12])
                del pending[prompt_id]
            else:
                queue.put_nowait((prompt_id, source))

        prompt_hashes: dict[str, str] = {}

        async def worker() -> None:
            while not queue.empty():
                prompt_id, source = queue.get_nowait()
                try:
                    image = await read_image(source, http, max_bytes=max_prompt_bytes)
                    # Older indexes used the image hash as the prompt ID.
                    image_hash = hashlib.sha256(image).hexdigest()
                    prompt_hashes[prompt_id] = image_hash
                    if image_hash in submitted:
                        logger.info("{} | already submitted", prompt_id[:12])
                        del pending[prompt_id]
                        continue
                    logger.info("{} | generating", prompt_id[:12])

                    result = await generate(image, seed, client)
                    await client.submit(image, result)

                    logger.info("{} | submitted", prompt_id[:12])

                    del pending[prompt_id]
                except Exception:  # noqa: BLE001
                    logger.exception("Prompt {} failed", prompt_id)

        await asyncio.gather(*(worker() for _ in range(workers)))
        # A failed submission request may already have saved a terminal outcome.
        if pending:
            submitted = await read_saved(http)
            for prompt_id in list(pending):
                if prompt_id in submitted or prompt_hashes.get(prompt_id) in submitted:
                    del pending[prompt_id]

    if pending:
        raise RuntimeError(f"{len(pending)} prompts remain after one pass")


async def run(
    generate: Callable[[bytes, int, AsyncWitnessClient], Awaitable[str | None]],
    prompts: Mapping[str, str | bytes],
    seed: int,
    *,
    witness_config: Path,
    read_saved: Callable[[httpx.AsyncClient], Awaitable[set[str]]],
    workers: int = 8,
    parallel_calls: int = 3,
    parallel_preparations: int = 3,
    attempts: int = 3,
    wallet_name: str = "miner",
    wallet_hotkey: str = "default",
    wallet_path: str = "~/.bittensor/wallets",
    api: bool = False,
) -> None:
    """Run pending prompts with a fresh client for each configured attempt."""
    remaining = dict(prompts)
    if not remaining:
        return
    config = cast(WitnessConfig, json.loads(witness_config.read_bytes()))
    hotkey = Wallet(name=wallet_name, hotkey=wallet_hotkey, path=str(Path(wallet_path).expanduser())).hotkey

    for attempt in range(1, attempts + 1):
        logger.info("Attempt {}/{} | {} prompts remaining", attempt, attempts, len(remaining))
        try:
            async with AsyncWitnessClient(
                config,
                hotkey=hotkey,
                openai_api_key=os.environ["OPENAI_API_KEY"] if api else None,
                subscription=None if api else "codex",
                parallel_calls=parallel_calls,
                parallel_preparations=parallel_preparations,
            ) as witness:
                await run_pass(generate, remaining, seed, client=witness, read_saved=read_saved, workers=workers)
        except Exception:  # noqa: BLE001
            logger.exception("Attempt {}/{} failed", attempt, attempts)
        if not remaining:
            return

    raise RuntimeError(f"{len(remaining)} prompts remain after {attempts} attempts")

"""Reference miner service implementing the batch generation API.

Endpoints:
    GET  /health   — Basic health check (used during pod warmup by orchestrator)
    GET  /status   — Pod status (warming_up → ready → generating → complete → ready/replace)
    POST /generate — Submit a batch of prompt image URLs for generation
    GET  /results  — Download completed batch results as a ZIP archive

This is a reference implementation. A real miner would replace the placeholder
generation with actual model inference.
"""

import asyncio
import io
import json
import time
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from miner_reference.threejs_placeholder import generate_car_scene
from miner_reference.vram import EXPECTED_TOTAL_VRAM_GB, check_vram_adequate


class PodStatus(StrEnum):
    WARMING_UP = "warming_up"
    READY = "ready"
    GENERATING = "generating"
    COMPLETE = "complete"
    REPLACE = "replace"


class PromptItem(BaseModel):
    stem: str
    image_url: str


class GenerateBatchRequest(BaseModel):
    prompts: list[PromptItem]
    seed: int


class StatusResponse(BaseModel):
    status: PodStatus
    progress: int | None = None
    total: int | None = None
    payload: dict | None = None


class GenerateAcceptedResponse(BaseModel):
    accepted: int


class MinerState:
    """Tracks the current state of the miner pod."""

    def __init__(self) -> None:
        self.status: PodStatus = PodStatus.WARMING_UP
        self.prompts: list[PromptItem] = []
        self.batch_stems: frozenset[str] = frozenset()
        self.results: dict[str, bytes] = {}
        self.failed: dict[str, str] = {}
        self.progress: int = 0
        self.total: int = 0
        self.vram_ok: bool = True
        self.vram_total_gb: float | None = None
        self._generation_task: asyncio.Task | None = None  # type: ignore[type-arg]

    def reset_batch(self) -> None:
        self.prompts = []
        self.batch_stems = frozenset()
        self.results = {}
        self.failed = {}
        self.progress = 0
        self.total = 0


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Check VRAM and simulate model loading on startup."""
    state = _app.state.miner
    logger.info("Starting up — checking VRAM and loading models...")

    state.vram_ok, state.vram_total_gb = check_vram_adequate()
    if not state.vram_ok:
        if state.vram_total_gb is not None:
            logger.warning(
                f"VRAM check failed: detected {state.vram_total_gb:.1f} GB, "
                f"expected ~{EXPECTED_TOTAL_VRAM_GB} GB (4x H200 SXM)"
            )
        else:
            logger.warning(
                f"VRAM check failed: could not detect GPUs, "
                f"expected ~{EXPECTED_TOTAL_VRAM_GB} GB (4x H200 SXM)"
            )
    else:
        logger.info(f"VRAM OK: {state.vram_total_gb:.1f} GB detected")

    # Simulate model loading time — a real miner loads models here
    await asyncio.sleep(2)
    state.status = PodStatus.READY
    logger.info("Models loaded — ready for batches")
    yield


def create_app() -> FastAPI:
    application = FastAPI(title="Miner Reference", lifespan=lifespan)
    application.state.miner = MinerState()
    return application


app = create_app()


def _get_state() -> MinerState:
    return app.state.miner


@app.get("/health")
async def health() -> dict[str, str]:
    """Basic health check. Returns 200 when the service is running."""
    return {"status": "ok"}


@app.get("/status")
async def status(replacements_remaining: int = Query(default=0)) -> StatusResponse:
    """Report current pod status.

    The orchestrator polls this endpoint continuously.
    `replacements_remaining` tells us how many pod replacements are left in the budget.

    Demonstrates VRAM-based replacement request:
    - If VRAM is inadequate AND replacements are available, request a replacement.
    - If VRAM is inadequate but no replacements left, soldier on with what we have.
    """
    state = _get_state()

    # Request replacement on bad hardware — check during warming_up and ready
    if state.status in (PodStatus.WARMING_UP, PodStatus.READY) and not state.vram_ok:
        if replacements_remaining > 0:
            logger.warning(
                f"Requesting pod replacement: VRAM {state.vram_total_gb} GB "
                f"is below expected {EXPECTED_TOTAL_VRAM_GB} GB. "
                f"Replacements remaining: {replacements_remaining}"
            )
            return StatusResponse(
                status=PodStatus.REPLACE,
                payload={
                    "reason": "insufficient_vram",
                    "detected_vram_gb": state.vram_total_gb,
                    "expected_vram_gb": EXPECTED_TOTAL_VRAM_GB,
                },
            )
        else:
            logger.warning(
                f"VRAM is insufficient ({state.vram_total_gb} GB) but no replacements left — continuing anyway"
            )

    if state.status == PodStatus.GENERATING:
        return StatusResponse(
            status=PodStatus.GENERATING,
            progress=state.progress,
            total=state.total,
        )

    return StatusResponse(status=state.status)


@app.post("/generate")
async def generate(request: GenerateBatchRequest) -> GenerateAcceptedResponse:
    """Accept a batch of prompts and start generating.

    Accepts from `ready` (first batch) or `complete` (subsequent batches).
    Generation runs in the background — poll /status for progress.
    """
    state = _get_state()

    incoming_stems = frozenset(p.stem for p in request.prompts)

    if state.status in (PodStatus.GENERATING, PodStatus.COMPLETE) and incoming_stems == state.batch_stems:
        logger.info(f"Idempotent /generate retry detected ({len(incoming_stems)} prompts)")
        return GenerateAcceptedResponse(accepted=len(request.prompts))

    if state.status not in (PodStatus.READY, PodStatus.COMPLETE):
        raise HTTPException(
            status_code=409,
            detail={"detail": "Cannot accept batch", "current_status": state.status},
        )

    state.prompts = request.prompts
    state.batch_stems = incoming_stems
    state.results = {}
    state.failed = {}
    state.progress = 0
    state.total = len(request.prompts)
    state.status = PodStatus.GENERATING

    logger.info(f"Received batch of {state.total} prompts (seed={request.seed})")

    state._generation_task = asyncio.create_task(_run_generation(state, request.seed))
    return GenerateAcceptedResponse(accepted=state.total)


RESULTS_CHUNK_SIZE = 64 * 1024  # 64 KB chunks for streaming


@app.get("/results")
async def results() -> StreamingResponse:
    """Download completed batch results as a streamed ZIP archive.

    Each successful prompt becomes a {stem}.js entry. If any prompts failed,
    a _failed.json manifest is included. The orchestrator calls this after
    /status returns `complete`.

    Uses chunked transfer encoding to avoid connection drops from reverse
    proxies that enforce response size limits or idle timeouts.
    """
    state = _get_state()

    if state.status != PodStatus.COMPLETE:
        raise HTTPException(
            status_code=409, detail=f"Results not available in state '{state.status}'. Must be 'complete'."
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for stem, data in state.results.items():
            zf.writestr(f"{stem}.js", data)
        if state.failed:
            zf.writestr("_failed.json", json.dumps(state.failed, indent=2))

    total_size = buf.tell()
    buf.seek(0)
    served = len(state.results)

    async def stream_zip() -> AsyncIterator[bytes]:
        while True:
            chunk = buf.read(RESULTS_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
        logger.info(f"Streamed {served} results ({total_size} bytes), staying in complete until next /generate")

    return StreamingResponse(stream_zip(), media_type="application/zip")


async def _run_generation(state: MinerState, seed: int) -> None:
    """Background task that generates Three.js scenes for each prompt.

    A real miner would:
    - Load prompt images from URLs
    - Run inference on GPUs (parallelized across 4x H200)
    - Handle failures per-prompt (skip and return partial results)
    """
    logger.info(f"Starting generation for {len(state.prompts)} prompts")
    start = time.monotonic()

    for i, prompt in enumerate(state.prompts):
        # In a real miner, this is where you'd:
        # 1. Download the prompt image from prompt.image_url
        # 2. Run your 3D generation model
        # 3. Handle errors per-prompt (try/except, skip failures)

        try:
            scene_bytes = generate_car_scene()
            state.results[prompt.stem] = scene_bytes
        except Exception as e:
            state.failed[prompt.stem] = str(e)
            logger.error(f"Failed to generate for {prompt.stem}: {e}")

        state.progress = i + 1

        # Simulate generation time
        await asyncio.sleep(0.1)

    elapsed = time.monotonic() - start
    state.status = PodStatus.COMPLETE
    logger.info(f"Batch complete: {len(state.results)}/{len(state.prompts)} succeeded in {elapsed:.1f}s")

"""Reference miner service implementing the batch generation API.

Endpoints:
    GET  /health   — Basic health check (used during pod warmup by orchestrator)
    GET  /status   — Pod status (warming_up → ready → generating → complete → ...)
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

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from miner_reference.threejs_placeholder import generate_car_scene
from miner_reference.vram import EXPECTED_TOTAL_VRAM_GB, check_vram_adequate


class PodStatus(StrEnum):
    WARMING_UP = "warming_up"
    READY = "ready"
    GENERATING = "generating"
    COMPLETE = "complete"
    REPLACE = "replace"


# Per the API Spec: lowercase alphanumeric, unique within a round.
# Validating at the model level rejects malformed input with a 422 before
# any state mutation happens.
STEM_PATTERN = r"^[a-z0-9]+$"


class PromptItem(BaseModel):
    stem: str = Field(..., pattern=STEM_PATTERN)
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
        self.warming_up_stage: str | None = None  # demo of /status payload
        self.prompts: list[PromptItem] = []
        self.batch_stems: frozenset[str] = frozenset()
        self.results: dict[str, bytes] = {}
        self.failed: dict[str, str] = {}
        self.progress: int = 0
        self.total: int = 0
        # Built once when generation completes, served byte-identically on
        # every /results call until the next /generate. Avoids re-zipping on
        # retries and gives the orchestrator a stable response across drops.
        self.cached_zip_bytes: bytes | None = None
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
        self.cached_zip_bytes = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Check VRAM and simulate model loading on startup.

    Real miners load models here. This reference simulates a multi-stage
    warmup so the /status payload demo has something meaningful to report.
    """
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

    # Simulate a multi-stage warmup. Real miners report actual stage names
    # like "loading_unet", "loading_vae", "compiling_kernels". The /status
    # handler reads `warming_up_stage` and surfaces it in the payload field.
    for stage_name, duration in [
        ("loading_models", 0.7),
        ("compiling_kernels", 0.7),
        ("warming_caches", 0.6),
    ]:
        state.warming_up_stage = stage_name
        await asyncio.sleep(duration)

    state.warming_up_stage = None
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
async def health(
    authorization: str | None = Header(default=None),  # noqa: ARG001
) -> dict[str, str]:
    """Basic health check. Returns 200 when the service is running.

    The Authorization header is captured but unused — see the Authentication
    section of the API spec for the forwarding pattern. Real miners pass it
    through to provider-specific APIs (e.g., Verda's GPU health endpoint).
    """
    return {"status": "ok"}


@app.get("/status", response_model_exclude_none=True)
async def status(
    replacements_remaining: int = Query(default=0),
    authorization: str | None = Header(default=None),  # noqa: ARG001
) -> StatusResponse:
    """Report current pod status.

    The orchestrator polls this endpoint continuously.
    `replacements_remaining` tells us how many pod replacements are left in
    the budget.

    Demonstrates two patterns from the spec:
    - VRAM-based replacement request when bad hardware is detected and
      replacements are available (suppressed when budget is 0 — returning
      `replace` then ends the round).
    - `payload` field used in non-replace states for diagnostic info,
      mirroring the `{"stage": "loading_unet", ...}` example in the spec.

    Note: `response_model_exclude_none=True` strips null fields from the
    JSON response, so `progress`/`total`/`payload` only appear when set.
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
        logger.warning(
            f"VRAM is insufficient ({state.vram_total_gb} GB) but no replacements left — continuing anyway"
        )

    if state.status == PodStatus.WARMING_UP and state.warming_up_stage is not None:
        return StatusResponse(
            status=PodStatus.WARMING_UP,
            payload={"stage": state.warming_up_stage},
        )

    if state.status == PodStatus.GENERATING:
        return StatusResponse(
            status=PodStatus.GENERATING,
            progress=state.progress,
            total=state.total,
        )

    return StatusResponse(status=state.status)


@app.post("/generate")
async def generate(
    request: GenerateBatchRequest,
    authorization: str | None = Header(default=None),  # noqa: ARG001
) -> GenerateAcceptedResponse:
    """Accept a batch of prompts and start generating.

    Accepts from `ready` (first batch) or `complete` (subsequent batches).
    Generation runs in the background — poll /status for progress.

    Idempotency: a retry that arrives while we're already processing the
    same set of stems returns 200 with the same accepted count, no work
    restart. The orchestrator relies on this to safely retry when the
    network drops a /generate response.
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
    state.cached_zip_bytes = None  # discard previous batch's cached results
    state.status = PodStatus.GENERATING

    logger.info(f"Received batch of {state.total} prompts (seed={request.seed})")

    state._generation_task = asyncio.create_task(_run_generation(state, request.seed))
    return GenerateAcceptedResponse(accepted=state.total)


RESULTS_CHUNK_SIZE = 64 * 1024  # 64 KB chunks for streaming


@app.get("/results")
async def results(
    authorization: str | None = Header(default=None),  # noqa: ARG001
) -> StreamingResponse:
    """Download completed batch results as a streamed ZIP archive.

    Each successful prompt becomes a {stem}.js entry. If any prompts failed,
    a _failed.json manifest is included. The orchestrator calls this after
    /status returns `complete`.

    The ZIP is built once when generation completes and cached on
    `MinerState.cached_zip_bytes`. Every /results call streams the same
    bytes, so retries (which are explicitly allowed by the spec) return a
    byte-identical archive without re-zipping. The service stays in
    `complete` until the next `/generate`, so /results is freely retryable.

    Uses chunked transfer encoding (no Content-Length header) to avoid
    connection drops from reverse proxies that enforce response size limits
    or idle timeouts on buffered responses.
    """
    state = _get_state()

    if state.status != PodStatus.COMPLETE:
        raise HTTPException(
            status_code=409,
            detail=f"Results not available in state '{state.status}'. Must be 'complete'.",
        )

    if state.cached_zip_bytes is None:
        # Defensive: should always be set when status == COMPLETE because
        # _run_generation builds it in the finally block. If we somehow get
        # here without a cached ZIP (e.g., the task was canceled before
        # finally ran), build an empty one rather than crashing.
        logger.warning("cached_zip_bytes missing in COMPLETE state — building empty archive")
        state.cached_zip_bytes = _build_results_zip(results_map={}, failed_map={})

    zip_bytes = state.cached_zip_bytes
    served = len(state.results)
    total_size = len(zip_bytes)

    async def stream_zip() -> AsyncIterator[bytes]:
        offset = 0
        while offset < total_size:
            chunk = zip_bytes[offset : offset + RESULTS_CHUNK_SIZE]
            offset += len(chunk)
            yield chunk
        logger.info(
            f"Streamed {served} results ({total_size} bytes), staying in complete until next /generate"
        )

    return StreamingResponse(stream_zip(), media_type="application/zip")


def _build_results_zip(results_map: dict[str, bytes], failed_map: dict[str, str]) -> bytes:
    """Pack a results dict and an optional failure manifest into a ZIP archive.

    Each successful prompt becomes a `{stem}.js` entry. If any prompts
    failed, a `_failed.json` manifest is added. Empty batches are valid:
    they produce a ZIP containing only `_failed.json` (or, if both maps are
    empty, an empty archive that the orchestrator treats as a complete
    batch failure).
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for stem, data in results_map.items():
            zf.writestr(f"{stem}.js", data)
        if failed_map:
            zf.writestr("_failed.json", json.dumps(failed_map, indent=2))
    return buf.getvalue()


async def _run_generation(state: MinerState, seed: int) -> None:
    """Background task that generates Three.js scenes for each prompt.

    A real miner would:
    - Load prompt images from URLs
    - Run inference on GPUs (parallelized across 4x H200)
    - Handle failures per-prompt (skip and return partial results)

    Crash safety: the entire body is wrapped in try/except/finally so the
    pod ALWAYS transitions to COMPLETE, even on a top-level failure.
    The spec is explicit: "If your generation task crashes, transition to
    `complete` with whatever partial results you have. A pod stuck in
    `generating` forever will be detected as unresponsive and replaced,
    costing a pod from your budget." This is the catch-all that enforces
    that contract — the per-prompt try/except above only protects against
    inner failures.
    """
    logger.info(f"Starting generation for {len(state.prompts)} prompts")
    start = time.monotonic()

    try:
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
    except Exception as e:
        # Top-level catch: if anything outside the per-prompt try/except
        # fails (loop termination, async cancellation propagating, state
        # mutation throwing, asyncio.sleep raising, etc.), mark every
        # un-recorded prompt as failed and fall through to finally so the
        # pod transitions to COMPLETE.
        logger.exception(f"Generation task crashed: {e}")
        for prompt in state.prompts:
            if prompt.stem not in state.results and prompt.stem not in state.failed:
                state.failed[prompt.stem] = f"generation crashed: {e}"
    finally:
        elapsed = time.monotonic() - start
        # Build the ZIP exactly once, here, while we still own the state.
        # Subsequent /results calls stream from this cached buffer.
        state.cached_zip_bytes = _build_results_zip(state.results, state.failed)
        state.status = PodStatus.COMPLETE
        logger.info(
            f"Batch complete: {len(state.results)}/{len(state.prompts)} succeeded "
            f"in {elapsed:.1f}s ({len(state.cached_zip_bytes)} byte archive cached)"
        )

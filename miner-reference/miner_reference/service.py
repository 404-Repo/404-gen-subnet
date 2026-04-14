"""Reference miner service implementing the batch generation API.

Endpoints:
    GET  /health   — Basic health check (used during pod warmup by orchestrator)
    GET  /status   — Pod status (warming_up → ready → generating → complete → ...)
    POST /generate — Submit a batch of prompt image URLs for generation
    GET  /results  — Download completed batch results as a ZIP archive

See api_specification.md for the full protocol contract.
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
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from miner_reference.threejs_placeholder import generate_car_scene


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
        self.gpu_degraded: bool = False
        self.benchmark_payload: dict | None = None
        self._generation_task: asyncio.Task | None = None  # type: ignore[type-arg]

    def reset_batch(self) -> None:
        self.prompts = []
        self.batch_stems = frozenset()
        self.results = {}
        self.failed = {}
        self.progress = 0
        self.total = 0
        self.cached_zip_bytes = None
        self.gpu_degraded = False
        self.benchmark_payload = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan runs BEFORE the server accepts connections, so the
    orchestrator cannot poll /health or /status during this phase. For a
    service with long startup, yield immediately and run initialization as
    a background task that transitions the pod to ``ready`` when done.
    """
    state = _app.state.miner
    logger.info("Starting up...")

    await _run_gpu_benchmark(state)

    state.status = PodStatus.READY
    logger.info("Ready for batches")
    yield


def create_app() -> FastAPI:
    application = FastAPI(title="Miner Reference", lifespan=lifespan)
    application.state.miner = MinerState()
    return application


app = create_app()


def _get_state() -> MinerState:
    state: MinerState = app.state.miner
    return state


@app.get("/health")
async def health(
    authorization: str | None = Header(default=None),  # noqa: ARG001
) -> dict[str, str]:
    """Basic health check. Returns 200 when the service is running.

    The ``Authorization`` header is accepted but unused here. See the
    Authentication section of api_specification.md for when to forward it.
    """
    return {"status": "ok"}


@app.get("/status", response_model_exclude_none=True)
async def status(
    replacements_remaining: int = Query(default=0),
    authorization: str | None = Header(default=None),  # noqa: ARG001
) -> StatusResponse:
    """Report current pod status.

    The orchestrator polls this continuously. ``replacements_remaining``
    tells us how many pod replacements are still available in the budget.

    ``response_model_exclude_none=True`` strips null fields from the JSON
    response, so ``progress``/``total``/``payload`` only appear when set.
    """
    state = _get_state()

    # Replace on degraded hardware (benchmark checks compute + VRAM per GPU).
    if state.gpu_degraded:
        if replacements_remaining > 0:
            logger.warning(
                f"Requesting pod replacement: GPU benchmark failed. "
                f"Replacements remaining: {replacements_remaining}"
            )
            return StatusResponse(
                status=PodStatus.REPLACE,
                payload=state.benchmark_payload,
            )
        logger.warning("GPU benchmark failed but no replacements left — pushing through")

    if state.status == PodStatus.GENERATING:
        return StatusResponse(
            status=PodStatus.GENERATING,
            progress=state.progress,
            total=state.total,
            payload=state.benchmark_payload,
        )

    return StatusResponse(status=state.status, payload=state.benchmark_payload)


@app.post("/generate", response_model=GenerateAcceptedResponse)
async def generate(
    request: GenerateBatchRequest,
    authorization: str | None = Header(default=None),  # noqa: ARG001
) -> GenerateAcceptedResponse | JSONResponse:
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
        # Return JSONResponse directly (not HTTPException). FastAPI's
        # HTTPException wraps its `detail` argument under a top-level
        # `detail` key, which would produce the nested shape
        # `{"detail": {"detail": "...", "current_status": "..."}}`.
        # The API spec requires the flat shape
        # `{"detail": "...", "current_status": "..."}`, so we build the
        # response body ourselves and bypass the exception wrapping.
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Cannot accept batch",
                "current_status": state.status.value,
            },
        )

    state.prompts = request.prompts
    state.batch_stems = incoming_stems
    state.results = {}
    state.failed = {}
    state.progress = 0
    state.total = len(request.prompts)
    state.cached_zip_bytes = None  # discard previous batch's cached results
    state.gpu_degraded = False
    state.benchmark_payload = None
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
        logger.info(f"Streamed {served} results ({total_size} bytes), staying in complete until next /generate")

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
    """Background task: benchmark GPUs, then produce results.

    1. Runs the GPU health benchmark (compute + VRAM). If any GPU is
       degraded, ``state.gpu_degraded`` is set and the task pauses 15 s
       so the orchestrator can poll ``/status`` and act on the ``replace``
       signal before generation proceeds.
    2. Produces a result for every prompt (placeholder car.js in this
       reference). Runs even when the benchmark failed so the pod can
       push through if no replacements are left.

    Wrapped in try/except/finally so the pod ALWAYS transitions to
    COMPLETE — a pod stranded in GENERATING is treated as unresponsive
    and costs a replacement from the budget.
    """
    logger.info(f"Starting generation for {len(state.prompts)} prompts")
    start = time.monotonic()

    try:
        await _run_gpu_benchmark(state)

        if state.gpu_degraded:
            # Give the orchestrator a chance to poll /status and see the
            # replace signal before we finish.  If it tears the pod down,
            # this sleep (and everything after it) is simply interrupted.
            await asyncio.sleep(15)

        for i, prompt in enumerate(state.prompts):
            try:
                scene_bytes = generate_car_scene()
                state.results[prompt.stem] = scene_bytes
            except Exception as e:
                state.failed[prompt.stem] = str(e)
                logger.error(f"Failed to generate for {prompt.stem}: {e}")

            state.progress = i + 1
            await asyncio.sleep(0)
    except Exception as e:
        logger.exception(f"Generation task crashed: {e}")
        for prompt in state.prompts:
            if prompt.stem not in state.results and prompt.stem not in state.failed:
                state.failed[prompt.stem] = f"generation crashed: {e}"
    finally:
        elapsed = time.monotonic() - start
        state.cached_zip_bytes = _build_results_zip(state.results, state.failed)
        state.status = PodStatus.COMPLETE
        logger.info(
            f"Batch complete: {len(state.results)}/{len(state.prompts)} succeeded "
            f"in {elapsed:.1f}s ({len(state.cached_zip_bytes)} byte archive cached)"
        )


async def _run_gpu_benchmark(state: MinerState) -> None:
    """Run the GPU health benchmark (compute + VRAM), updating *state*.

    Sets ``state.gpu_degraded = True`` (plus a diagnostic payload) when any
    GPU fails. Requires torch with CUDA — the Docker image must include it.
    """
    try:
        import torch  # noqa: F401

        from miner_reference.gpu_benchmark import MIN_TFLOPS, MIN_VRAM_GB, run_benchmark
    except ImportError:
        logger.warning("torch not available — skipping GPU benchmark")
        return

    logger.info("Running GPU benchmark (compute + VRAM)...")
    results = await asyncio.to_thread(run_benchmark)

    for r in results:
        status_icon = "OK" if r.passed else "FAIL"
        logger.info(f"  GPU {r.gpu_id} ({r.gpu_name}): {r.tflops} TFLOPS, {r.vram_gb} GB VRAM — {status_icon}")

    failed = [r for r in results if not r.passed]

    state.benchmark_payload = {
        "benchmark": "gpu_health",
        "threshold_tflops": MIN_TFLOPS,
        "threshold_vram_gb": MIN_VRAM_GB,
        "all_passed": len(failed) == 0,
        "gpus": [
            {
                "gpu_id": r.gpu_id,
                "gpu_name": r.gpu_name,
                "tflops": r.tflops,
                "compute_passed": r.compute_passed,
                "vram_gb": r.vram_gb,
                "vram_passed": r.vram_passed,
                "passed": r.passed,
            }
            for r in results
        ],
    }

    if failed:
        state.gpu_degraded = True
        logger.warning(f"GPU benchmark failed for {len(failed)}/{len(results)} GPUs")
    else:
        logger.info("GPU benchmark passed — all GPUs healthy")

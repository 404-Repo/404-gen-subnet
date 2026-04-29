import asyncio
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
from loguru import logger
from subnet_common.competition.generations import GenerationResult
from subnet_common.embeddings import build_embeddings_npz
from subnet_common.r2_client import R2Client
from subnet_common.render import GRAY_BG, GRAY_VIEWS, WHITE_BG, WHITE_VIEWS, render_grid, render_views
from tenacity import RetryCallState, retry, stop_after_attempt, wait_exponential

from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.pod_client import (
    BatchResults,
    PodStatus,
    check_pod_status,
    download_batch,
    submit_batch,
)
from generation_orchestrator.prompts import Prompt
from generation_orchestrator.settings import Settings


MAX_PAYLOAD_JSON_BYTES = 4096


class ReplaceReason(StrEnum):
    """Why the pod should be swapped for a new one."""

    REQUESTED = "requested"  # miner asked via /status -> replace
    UNREACHABLE = "unreachable"  # /status repeatedly unresponsive
    SUBMISSION_FAILED = "submission_failed"
    DOWNLOAD_FAILED = "download_failed"
    BATCH_TIME_LIMIT = "batch_time_limit"


@dataclass
class BatchComplete:
    """One batch processed successfully. `generations` is the delta for this batch only."""

    generations: dict[str, GenerationResult]
    batch_time: float


@dataclass
class PodReplaceRequested:
    """Pod should be swapped. `payload` is the miner's diagnostic dict if any."""

    reason: ReplaceReason
    payload: dict | None = None


class PodSession:
    """One pod's session. Open the context, then `run(batch)` per batch.

    The pod is expected to be `/status=ready` before the session opens — warmup is the
    `GPUProviderManager.get_healthy_pod` contract. The session owns HTTP clients (R2,
    service, pod). It does not own batching, prior generations, or persistence — the
    caller feeds one batch at a time and persists the returned deltas.
    """

    _r2: R2Client
    _service_client: httpx.AsyncClient

    def __init__(
        self,
        *,
        settings: Settings,
        pod_endpoint: str,
        auth_token: str | None,
        hotkey: str,
        seed: int,
        stop: GenerationStop,
        remaining_replacements: int,
    ):
        self._settings = settings
        self._pod_endpoint = pod_endpoint
        self._auth_token = auth_token
        self._hotkey = hotkey
        self._seed = seed
        self._stop = stop
        self._remaining_replacements = remaining_replacements
        self._log_id = hotkey[:10]

    async def __aenter__(self) -> Self:
        self._r2 = R2Client(
            access_key_id=self._settings.r2_access_key_id.get_secret_value(),
            secret_access_key=self._settings.r2_secret_access_key.get_secret_value(),
            r2_endpoint=self._settings.r2_endpoint.get_secret_value(),
        )
        await self._r2.__aenter__()

        read = self._settings.render_timeout_seconds
        service_timeout = httpx.Timeout(connect=60.0, read=read, write=read, pool=60.0)
        self._service_client = httpx.AsyncClient(timeout=service_timeout)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._service_client.aclose()
        await self._r2.__aexit__(exc_type, exc, tb)

    async def run(self, round_num: int, batch: list[Prompt]) -> BatchComplete | PodReplaceRequested:
        """Submit a batch, poll until complete, download + process. Returns the delta or a swap signal."""
        batch_start = asyncio.get_running_loop().time()

        if not await submit_batch(
            endpoint=self._pod_endpoint,
            prompts=[(p.stem, p.url) for p in batch],
            seed=self._seed,
            auth_token=self._auth_token,
        ):
            logger.error(f"{self._log_id}: batch submission failed")
            return PodReplaceRequested(reason=ReplaceReason.SUBMISSION_FAILED)

        terminal, payload, give_up = await self._poll_status(
            start=batch_start,
            accept={PodStatus.COMPLETE},
            time_limit=self._settings.batch_time_limit_seconds,
        )
        if terminal == PodStatus.REPLACE:
            return PodReplaceRequested(reason=ReplaceReason.REQUESTED, payload=payload)
        if terminal != PodStatus.COMPLETE:
            return PodReplaceRequested(reason=give_up or ReplaceReason.UNREACHABLE)

        results = await download_batch(
            endpoint=self._pod_endpoint,
            auth_token=self._auth_token,
            timeout=self._settings.download_timeout_seconds,
            max_zip_bytes=self._settings.max_batch_zip_bytes,
            max_files=self._settings.max_batch_files,
            max_js_bytes=self._settings.max_js_bytes,
            max_failed_manifest_bytes=self._settings.max_failed_manifest_bytes,
        )
        batch_elapsed = asyncio.get_running_loop().time() - batch_start

        if results is None:
            logger.error(f"{self._log_id}: batch download failed")
            return PodReplaceRequested(reason=ReplaceReason.DOWNLOAD_FAILED)

        generations = await self._process_results(round_num, batch, results)
        logger.info(
            f"{self._log_id}: batch completed in {batch_elapsed:.1f}s "
            f"({len(results.successes)}/{len(batch)} successes, {len(results.failures)} failures)"
        )
        return BatchComplete(generations=generations, batch_time=batch_elapsed)

    async def _poll_status(
        self,
        start: float,
        accept: set[PodStatus],
        time_limit: float | None,
    ) -> tuple[PodStatus | None, dict | None, ReplaceReason | None]:
        """Poll until a status in `accept`, REPLACE, or give up.

        Returns `(status, payload, None)` on success (status is in `accept` or `REPLACE`),
        `(None, None, reason)` when we give up on a non-stop condition, or
        `(None, None, None)` when the stop signal fires.
        """
        consecutive_unreachable = 0
        interval = self._settings.status_check_interval_seconds
        max_unreachable = self._settings.max_consecutive_unhealthy

        while not self._stop.should_stop:
            if time_limit is not None:
                elapsed = asyncio.get_running_loop().time() - start
                if elapsed > time_limit:
                    logger.warning(f"{self._log_id}: batch time limit ({time_limit:.0f}s) exceeded")
                    return None, None, ReplaceReason.BATCH_TIME_LIMIT

            response = await check_pod_status(
                endpoint=self._pod_endpoint,
                auth_token=self._auth_token,
                replacements_remaining=self._remaining_replacements,
            )

            if response is None:
                consecutive_unreachable += 1
                logger.debug(f"{self._log_id}: status check failed ({consecutive_unreachable}/{max_unreachable})")
                if consecutive_unreachable >= max_unreachable:
                    logger.warning(f"{self._log_id}: pod unreachable after {max_unreachable} checks")
                    return None, None, ReplaceReason.UNREACHABLE
            else:
                consecutive_unreachable = 0
                if response.status in accept:
                    return response.status, _validated_payload(response.payload, self._log_id), None
                if response.status == PodStatus.REPLACE:
                    logger.info(f"{self._log_id}: pod requests replacement")
                    return PodStatus.REPLACE, _validated_payload(response.payload, self._log_id), None

            await self._stop.wait(timeout=interval)

        return None, None, None

    async def _process_results(
        self,
        round_num: int,
        batch: list[Prompt],
        results: BatchResults,
    ) -> dict[str, GenerationResult]:
        """Build one GenerationResult per batch stem (success or miner failure)."""
        batch_stems = {p.stem for p in batch}
        prompt_paths: dict[str, Path] = {p.stem: p.path for p in batch}

        extra = (set(results.successes) | set(results.failures)) - batch_stems
        for stem in extra:
            logger.warning(f"{self._log_id}: unexpected stem '{stem}' in results; ignoring")
        both = set(results.successes) & set(results.failures)
        for stem in both:
            logger.warning(f"{self._log_id}: stem '{stem}' in both successes and failures; treating as failure")

        new: dict[str, GenerationResult] = {}

        for stem, js_bytes in results.successes.items():
            if stem not in batch_stems or stem in both:
                continue
            new[stem] = await self._process_one_result(round_num, stem, js_bytes, prompt_paths[stem])

        for stem, reason in results.failures.items():
            if stem not in batch_stems:
                continue
            new[stem] = GenerationResult(failure_reason=reason)

        missing = batch_stems - set(results.successes) - set(results.failures)
        for stem in missing:
            logger.warning(f"{self._log_id}: stem '{stem}' missing from results; recording as miner failure")
            new[stem] = GenerationResult(failure_reason="missing from batch results")

        return new

    async def _process_one_result(
        self,
        round_num: int,
        stem: str,
        js_bytes: bytes,
        prompt_path: Path,
    ) -> GenerationResult:
        """Upload the JS module, render the 12 view PNGs + grid, compute embeddings.

        Atomic over the preview bundle: if any of {12 views, grid, embeddings} fails to
        render or upload, `views` stays None so the judge treats the stem as preview-less.
        """
        log_id = f"{self._log_id} / {stem}"
        result = GenerationResult(size=len(js_bytes))

        result.js = await self._upload(round_num, f"{stem}.js", js_bytes, log_id, kind="JS")

        render_key = self._settings.render_api_key.get_secret_value() if self._settings.render_api_key else None
        white, gray, grid = await asyncio.gather(
            render_views(
                self._service_client,
                self._settings.render_service_url,
                js_bytes,
                WHITE_VIEWS,
                WHITE_BG,
                log_id,
                api_key=render_key,
            ),
            render_views(
                self._service_client,
                self._settings.render_service_url,
                js_bytes,
                GRAY_VIEWS,
                GRAY_BG,
                log_id,
                api_key=render_key,
            ),
            render_grid(
                self._service_client,
                self._settings.render_service_url,
                js_bytes,
                log_id,
                api_key=render_key,
            ),
        )
        if white is None or gray is None or grid is None:
            return result

        try:
            embeddings_npz = await self._build_embeddings_npz(prompt_path, white, log_id)
        except Exception as e:
            logger.error(f"{log_id}: embeddings computation failed: {e}; dropping views for this stem")
            return result

        # Upload everything in parallel; abort the whole bundle on any failure.
        uploads: list[tuple[str, bytes, str]] = (
            [(f"{stem}/white/{view.name}.png", white[view.name], "VIEW") for view in WHITE_VIEWS]
            + [(f"{stem}/gray/{view.name}.png", gray[view.name], "VIEW") for view in GRAY_VIEWS]
            + [
                (f"{stem}/grid.png", grid, "GRID"),
                (f"{stem}/embeddings.npz", embeddings_npz, "EMB"),
            ]
        )
        urls = await asyncio.gather(
            *[self._upload(round_num, filename, data, log_id, kind=kind) for filename, data, kind in uploads]
        )
        if any(u is None for u in urls):
            logger.warning(f"{log_id}: at least one preview upload failed; dropping views for this stem")
            return result

        result.views = self._views_prefix(round_num, stem)
        return result

    async def _build_embeddings_npz(self, prompt_path: Path, white_views: dict[str, bytes], log_id: str) -> bytes:
        """Read prompt bytes, then delegate to the shared embeddings/npz packer."""
        prompt_bytes = await asyncio.to_thread(prompt_path.read_bytes)
        hf_token = self._settings.hf_token.get_secret_value() if self._settings.hf_token else None
        return await build_embeddings_npz(
            prompt_bytes, white_views, log_id, revision=self._settings.dinov3_revision, hf_token=hf_token
        )

    def _views_prefix(self, round_num: int, stem: str) -> str:
        """CDN URL of the per-stem folder; views live under `{prefix}/white/...` and `{prefix}/gray/...`."""
        key = self._settings.storage_key_template.format(round=round_num, hotkey=self._hotkey, filename=stem)
        return f"{self._settings.cdn_url}/{key}"

    async def _upload(self, round_num: int, filename: str, data: bytes, log_id: str, *, kind: str) -> str | None:
        """Upload data to R2 and return CDN URL. Retries up to 3 times on transient errors."""
        r2 = self._r2
        key = self._settings.storage_key_template.format(round=round_num, hotkey=self._hotkey, filename=filename)

        def _log_retry(state: RetryCallState) -> None:
            exc = state.outcome.exception() if state.outcome else None
            logger.warning(f"{log_id}: {kind} upload retry {state.attempt_number}/3: {exc}")

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            before_sleep=_log_retry,
            reraise=True,
        )
        async def _do_upload() -> None:
            await r2.upload(key=key, data=data)

        try:
            await _do_upload()
            logger.debug(f"{log_id}: uploaded {key} ({len(data) / 1024:.1f}KB)")
            return f"{self._settings.cdn_url}/{key}"
        except Exception as e:
            logger.error(f"{log_id}: {kind} upload failed after retries: {e}")
            return None


def _validated_payload(payload: dict | None, log_id: str) -> dict | None:
    """Pass through the miner's payload if it's reasonably small; otherwise drop."""
    if payload is None:
        return None
    try:
        size = len(json.dumps(payload).encode("utf-8"))
    except (TypeError, ValueError):
        logger.warning(f"{log_id}: payload is not JSON-serializable; dropping")
        return None
    if size > MAX_PAYLOAD_JSON_BYTES:
        logger.warning(f"{log_id}: payload too large ({size} bytes > {MAX_PAYLOAD_JSON_BYTES}); dropping")
        return None
    return payload

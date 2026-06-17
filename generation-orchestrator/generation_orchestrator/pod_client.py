import io
import json
import zipfile
from dataclasses import dataclass, field
from enum import StrEnum

import httpx
from loguru import logger
from pydantic import BaseModel
from tenacity import RetryCallState, retry, retry_if_exception, stop_after_attempt, wait_exponential


class PodStatus(StrEnum):
    """Pod lifecycle states reported by `GET /status` (see miner-reference/api_specification.md)."""

    WARMING_UP = "warming_up"
    READY = "ready"
    GENERATING = "generating"
    COMPLETE = "complete"
    REPLACE = "replace"


class PodStatusResponse(BaseModel):
    status: PodStatus
    progress: int | None = None
    total: int | None = None
    payload: dict | None = None


@dataclass
class BatchResults:
    """Successful and failed stems from a single `GET /results` download."""

    successes: dict[str, bytes] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


def _is_retryable(exc: BaseException) -> bool:
    """Retry on transient network errors and server-side HTTP failures.

    Includes 400 / 429 / 430 alongside 5xx: serverless proxies (Runpod) emit 400 on
    cold-start races and 430 on transient capacity rejects; both succeed on retry.
    Real client-error 4xx (401/403/422/etc.) are still treated as deterministic.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code in (400, 429, 430)
    if isinstance(exc, httpx.RequestError):
        return True
    return False


def _log_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(f"pod_client retry {retry_state.attempt_number}/5: {exc}")


_retry = retry(
    stop=stop_after_attempt(5),
    retry=retry_if_exception(_is_retryable),
    # Spread waits 2 → 30s; total ≈ 2+4+8+16 ≈ 30s across the 4 retry pauses.
    wait=wait_exponential(multiplier=2, min=2, max=30),
    before_sleep=_log_retry,
    reraise=True,
)


@_retry
async def _submit_batch_once(
    endpoint: str,
    headers: dict[str, str],
    body: dict,
    timeout: float,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=timeout, write=timeout, pool=30.0)) as client:
        response = await client.post(
            f"{endpoint}/generate",
            json=body,
            headers=headers,
        )
        if response.status_code >= 500 or response.status_code in (400, 429, 430):
            response.raise_for_status()
        return response


async def submit_batch(
    endpoint: str,
    prompts: list[tuple[str, str]],
    seed: int,
    auth_token: str | None,
    timeout: float = 60.0,
) -> bool:
    """Submit a batch of prompts to the pod via `POST /generate`.

    Body: {"prompts": [{"stem", "image_url"}, ...], "seed"}.
    Returns True on 200 (batch accepted).
    Returns False on 409 (pod busy / warming up / replacing) or terminal error — the
    caller can re-check pod status to decide next steps.
    """
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    body = {
        "prompts": [{"stem": stem, "image_url": url} for stem, url in prompts],
        "seed": seed,
    }

    try:
        response = await _submit_batch_once(endpoint, headers, body, timeout)
    except httpx.HTTPStatusError as exc:
        logger.error(f"Batch submission HTTP {exc.response.status_code}")
        return False
    except httpx.TimeoutException:
        logger.warning("Batch submission timed out after retries")
        return False
    except httpx.RequestError as e:
        logger.warning(f"Batch submission request error after retries: {e}")
        return False

    if response.status_code == 200:
        return True

    if response.status_code == 409:
        try:
            detail = response.json()
            current_status = detail.get("current_status")
        except ValueError:
            current_status = None
        logger.warning(f"Batch submission rejected (409); pod current_status={current_status}")
        return False

    logger.error(f"Batch submission HTTP {response.status_code}")
    return False


_STATUS_PAYLOAD_LOG_CAP = 500


def _format_payload(payload: dict | None) -> str:
    """Serialize a /status payload for logs, trimmed to a fixed cap to bound noise/risk."""
    if payload is None:
        return "None"
    s = json.dumps(payload, default=str)
    if len(s) <= _STATUS_PAYLOAD_LOG_CAP:
        return s
    return f"{s[:_STATUS_PAYLOAD_LOG_CAP]}...[+{len(s) - _STATUS_PAYLOAD_LOG_CAP} bytes]"


async def check_pod_status(
    endpoint: str,
    auth_token: str | None,
    replacements_remaining: int,
    *,
    log_id: str,
    timeout: float = 30.0,
) -> PodStatusResponse | None:
    """Check pod status. Returns None on connection error (transient).

    No retry here — the caller polls on a fixed interval, which is its own retry loop.
    `log_id` is prefixed on every log line so operators can tie repeated polling noise
    back to the specific miner/pod (typically `hotkey[:10]` or the pod name).
    """
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{endpoint}/status",
                params={"replacements_remaining": replacements_remaining},
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            parsed = PodStatusResponse(
                status=PodStatus(data["status"]),
                progress=data.get("progress"),
                total=data.get("total"),
                payload=data.get("payload"),
            )
            logger.debug(
                f"{log_id}: status from {endpoint}: status={parsed.status} "
                f"progress={parsed.progress}/{parsed.total} payload={_format_payload(parsed.payload)}"
            )
            return parsed
    except (httpx.TimeoutException, httpx.ConnectError) as e:
        # Previously silent. Surface the cause so an operator looking at
        # "status check failed (N/M)" upstream knows whether it's a timeout vs
        # connect refusal vs cold-start race.
        logger.warning(f"{log_id}: status check {type(e).__name__} on {endpoint}: {e}")
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning(f"{log_id}: status check HTTP {exc.response.status_code} on {endpoint}")
        return None
    except httpx.RequestError as e:
        logger.warning(f"{log_id}: status check {type(e).__name__} on {endpoint}: {e}")
        return None
    except (KeyError, ValueError) as e:
        logger.warning(f"{log_id}: status check invalid response on {endpoint}: {type(e).__name__}: {e}")
        return None


@_retry
async def _download_batch_once(
    endpoint: str,
    headers: dict[str, str],
    timeout: float,
) -> bytes:
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=timeout, write=30.0, pool=30.0)) as client:
        response = await client.get(
            f"{endpoint}/results",
            headers=headers,
        )
        response.raise_for_status()
        return response.content


async def download_batch(
    endpoint: str,
    auth_token: str | None,
    max_zip_bytes: int,
    max_files: int,
    max_js_bytes: int,
    max_failed_manifest_bytes: int,
    timeout: float = 300.0,
) -> BatchResults | None:
    """Download batch results from `GET /results`.

    The pod returns a ZIP archive containing `{stem}.js` entries plus an optional
    `_failed.json` manifest (`{stem: reason}`). Returns `BatchResults` on success.

    The archive is bounded by caller-supplied caps: overall ZIP byte size, entry count,
    per-`.js` uncompressed size, and manifest size. A violation on any axis returns None
    — the pod is treated as hostile/buggy and the caller swaps it out.

    Returns None on terminal transport failure or if the archive is empty (no `.js`
    files and no `_failed.json`), which the spec treats as a complete batch failure.
    """
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        content = await _download_batch_once(endpoint, headers, timeout)
    except httpx.HTTPStatusError as exc:
        logger.error(f"Batch download HTTP {exc.response.status_code}")
        return None
    except httpx.TimeoutException:
        logger.warning("Batch download timed out after retries")
        return None
    except httpx.RequestError as e:
        logger.warning(f"Batch download request error after retries: {e}")
        return None

    if len(content) > max_zip_bytes:
        logger.error(f"Batch ZIP too large: {len(content)} bytes > cap {max_zip_bytes}")
        return None

    try:
        results = BatchResults()
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = zf.infolist()
            if len(infos) > max_files:
                logger.error(f"Batch archive has {len(infos)} entries > cap {max_files}")
                return None
            for zinfo in infos:
                basename = zinfo.filename.rsplit("/", 1)[-1]
                if basename == "_failed.json":
                    if zinfo.file_size > max_failed_manifest_bytes:
                        logger.error(
                            f"_failed.json too large: {zinfo.file_size} bytes > cap {max_failed_manifest_bytes}"
                        )
                        return None
                    try:
                        manifest = json.loads(zf.read(zinfo).decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as e:
                        logger.warning(f"Invalid _failed.json manifest: {e}")
                        continue
                    if not isinstance(manifest, dict):
                        logger.warning("_failed.json is not a JSON object")
                        continue
                    for stem, reason in manifest.items():
                        results.failures[str(stem)] = str(reason)
                elif basename.endswith(".js"):
                    if zinfo.file_size > max_js_bytes:
                        logger.error(f"{basename} too large: {zinfo.file_size} bytes > cap {max_js_bytes}")
                        return None
                    stem = basename.removesuffix(".js")
                    results.successes[stem] = zf.read(zinfo)
    except zipfile.BadZipFile:
        logger.error("Batch download returned invalid ZIP")
        return None

    if not results.successes and not results.failures:
        logger.error("Batch download returned empty archive")
        return None

    logger.debug(f"Downloaded batch: {len(results.successes)} successes, {len(results.failures)} failures")
    return results

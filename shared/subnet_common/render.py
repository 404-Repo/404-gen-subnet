"""Multi-view renderer client for the Three.js render service.

The render service exposes `POST /render` (see render-service-js/src/server.js). One
call renders N views in one shot — `thetas` and `phis` are pairwise lists, the
response is `{"images": [base64, ...], "count": N, ...}`.

The view sets and camera params here are the canonical contract used by both the
generation orchestrator and the submission collector. The judge reads PNGs back from
the storage layout `{prefix}/white/{view}.png` and `{prefix}/gray/{view}.png`.
"""

import asyncio
import base64
import binascii
from dataclasses import dataclass

import httpx
from loguru import logger
from tenacity import RetryCallState, retry, retry_if_exception, stop_after_attempt, wait_exponential


_RENDER_PATH = "/render"
_RENDER_GRID_PATH = "/render/grid"


@dataclass(frozen=True)
class View:
    name: str
    theta: float
    phi: float


# 8 white-bg views: 4 front-ish for prompt match, 4 back/sides/top for hidden-garbage detection.
WHITE_VIEWS: list[View] = [
    View("front_left", 30, 0),
    View("front_right", 330, 0),
    View("front_below", 0, 15),
    View("front_above", 0, -30),
    View("right", 90, 0),
    View("back", 180, 0),
    View("left", 270, 0),
    View("top_down", 0, -90),
]

# Gray-bg views: same four front-ish angles as the white set. Used for the gray-bg
# foreground rescue / fallback path. Kept symmetric with WHITE_VIEWS so embeddings,
# uploads, and judge stage references all align on the same labels.
GRAY_VIEWS: list[View] = [
    View("front_left", 30, 0),
    View("front_right", 330, 0),
    View("front_below", 0, 15),
    View("front_above", 0, -30),
]

WHITE_BG = "ffffff"
GRAY_BG = "808080"

# Friendly labels for log lines so we don't have to mentally decode hex.
_BG_LABELS = {WHITE_BG: "white", GRAY_BG: "gray"}

# Camera params are fixed across all views and both bg modes — see api_specification.md.
_RENDER_DEFAULTS = {
    "lighting": "follow",
    "img_size": "1024",
    "cam_radius": "2.0",
    "cam_fov_deg": "49.1",
}


def _is_retryable(exception: BaseException) -> bool:
    """Retry on 5xx, 400/429/430 (proxy/cold-start/rate-limit), timeouts, connection errors.

    400 is included because the render service itself never legitimately emits one
    (it only issues 404 for unknown routes and 422 for validation); any 400 we see
    is a serverless-proxy artifact (Runpod cold-start race / transient capacity
    reject) and retrying succeeds. Other 4xx (401/403/422/etc.) are still treated
    as deterministic rejections.
    """
    if isinstance(exception, httpx.HTTPStatusError):
        status = exception.response.status_code
        return status >= 500 or status in (400, 429, 430)
    return isinstance(exception, (httpx.TimeoutException, httpx.RequestError))


def _log_retry(retry_state: RetryCallState) -> None:
    log_id = retry_state.kwargs.get("log_id", "unknown")
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(f"{log_id}: render retry {retry_state.attempt_number}/3: {exception}")


def _build_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _build_query(views: list[View], bg_color: str) -> dict[str, str]:
    return {
        **_RENDER_DEFAULTS,
        "thetas": ",".join(str(v.theta) for v in views),
        "phis": ",".join(str(v.phi) for v in views),
        "bg_color": bg_color,
    }


@retry(
    stop=stop_after_attempt(5),
    retry=retry_if_exception(_is_retryable),
    # Spread waits 2 → 30s to give a cold-starting render worker time to come up.
    # Total budget ≈ 2+4+8+16 ≈ 30s of waiting across the 4 retry pauses.
    wait=wait_exponential(multiplier=2, min=2, max=30),
    before_sleep=_log_retry,
    reraise=True,
)
async def _render_views_with_retry(
    client: httpx.AsyncClient,
    endpoint: str,
    js_content: bytes,
    views: list[View],
    bg_color: str,
    log_id: str,
    api_key: str | None = None,
) -> dict[str, bytes]:
    """POST /render and decode the multi-image response into {view_name: png_bytes}.

    The render service returns raw PNG bytes when there's exactly one view and
    JSON `{"images": [base64, ...]}` for two or more — handle both even though
    real callers always pass 2+.
    """
    start = asyncio.get_running_loop().time()

    response = await client.post(
        f"{endpoint}{_RENDER_PATH}",
        params=_build_query(views, bg_color),
        json={"source": js_content.decode("utf-8")},
        headers=_build_headers(api_key),
    )
    response.raise_for_status()

    if len(views) == 1:
        images = [response.content]
    else:
        data = response.json()
        encoded = data.get("images")
        if not isinstance(encoded, list) or len(encoded) != len(views):
            raise ValueError(
                f"render returned {len(encoded) if isinstance(encoded, list) else 'no'} images, "
                f"expected {len(views)}"
            )
        try:
            images = [base64.b64decode(e) for e in encoded]
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"render returned non-base64 images: {e}") from e

    elapsed = asyncio.get_running_loop().time() - start
    total_kb = sum(len(p) for p in images) / 1024
    bg_label = _BG_LABELS.get(bg_color, bg_color)
    logger.debug(f"{log_id}: rendered {len(views)} {bg_label} views in {elapsed:.1f}s, {total_kb:.1f}KB")
    return {v.name: img for v, img in zip(views, images, strict=True)}


async def render_views(
    client: httpx.AsyncClient,
    endpoint: str,
    js_content: bytes,
    views: list[View],
    bg_color: str,
    log_id: str,
    api_key: str | None = None,
) -> dict[str, bytes] | None:
    """Render a batch of views in one call. Returns {view_name: png_bytes} or None on failure.

    Retries up to 3 times on 5xx/429/timeout. Validation failures from the render service
    (e.g. 422 for static-analysis rejection) are returned as None without retry — those
    are permanent miner failures, not transient errors.
    """
    try:
        return await _render_views_with_retry(
            client, endpoint, js_content, views, bg_color, log_id=log_id, api_key=api_key
        )
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if _is_retryable(e):
            logger.error(f"{log_id}: render failed after retries (status {status})")
        else:
            logger.warning(f"{log_id}: render rejected (status {status})")
        return None
    except (httpx.TimeoutException, httpx.RequestError):
        logger.error(f"{log_id}: render failed after retries (connection error)")
        return None
    except Exception as e:
        logger.exception(f"{log_id}: render failed: {e}")
        return None


@retry(
    stop=stop_after_attempt(5),
    retry=retry_if_exception(_is_retryable),
    # Spread waits 2 → 30s to give a cold-starting render worker time to come up.
    # Total budget ≈ 2+4+8+16 ≈ 30s of waiting across the 4 retry pauses.
    wait=wait_exponential(multiplier=2, min=2, max=30),
    before_sleep=_log_retry,
    reraise=True,
)
async def _render_grid_with_retry(
    client: httpx.AsyncClient,
    endpoint: str,
    js_content: bytes,
    log_id: str,
    api_key: str | None = None,
) -> bytes:
    """POST /render/grid and return the single composite PNG bytes."""
    start = asyncio.get_running_loop().time()

    response = await client.post(
        f"{endpoint}{_RENDER_GRID_PATH}",
        json={"source": js_content.decode("utf-8")},
        headers=_build_headers(api_key),
    )
    response.raise_for_status()

    elapsed = asyncio.get_running_loop().time() - start
    logger.debug(f"{log_id}: rendered grid in {elapsed:.1f}s, {len(response.content) / 1024:.1f}KB")
    return response.content


async def render_grid(
    client: httpx.AsyncClient,
    endpoint: str,
    js_content: bytes,
    log_id: str,
    api_key: str | None = None,
) -> bytes | None:
    """Render the 4-view 2x2 grid PNG via `POST /render/grid`. Returns None on failure.

    Used by the multi-stage judge's grid-based stages (artifact compare + checklist
    verify). Producers cache one grid PNG per stem.
    """
    try:
        return await _render_grid_with_retry(client, endpoint, js_content, log_id=log_id, api_key=api_key)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if _is_retryable(e):
            logger.error(f"{log_id}: grid render failed after retries (status {status})")
        else:
            logger.warning(f"{log_id}: grid render rejected (status {status})")
        return None
    except (httpx.TimeoutException, httpx.RequestError):
        logger.error(f"{log_id}: grid render failed after retries (connection error)")
        return None
    except Exception as e:
        logger.exception(f"{log_id}: grid render failed: {e}")
        return None


_WARMUP_JS = (
    "export default function generate(THREE) {"
    " const g = new THREE.Group();"
    " g.add(new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), new THREE.MeshBasicMaterial()));"
    " return g;"
    " }"
)


async def warmup(endpoint: str, timeout: float, api_key: str | None = None, log_id: str = "render-warmup") -> None:
    """Fire-and-forget dummy render to wake a possibly-cold serverless render service.

    Uses a single front view — minimal work, but a real `POST /render` so the worker
    actually spins up (GET /health is often served by the proxy and doesn't route to
    the worker). Errors are swallowed.
    """
    one_view = [WHITE_VIEWS[0]]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{endpoint}{_RENDER_PATH}",
                params=_build_query(one_view, WHITE_BG),
                json={"source": _WARMUP_JS},
                headers=_build_headers(api_key),
            )
        logger.debug(f"{log_id}: {endpoint}{_RENDER_PATH} warmup -> {response.status_code}")
    except Exception as e:
        logger.debug(f"{log_id}: warmup ping dropped ({e}); render_views will retry on first real call")

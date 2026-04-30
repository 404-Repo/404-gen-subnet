from __future__ import annotations

import io
import json
import zipfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from generation_orchestrator.pod_client import BatchResults, PodStatus, check_pod_status, download_batch, submit_batch


@pytest.fixture(autouse=True)
def _no_tenacity_sleep() -> Any:
    """Skip tenacity's between-retry waits so retry-exercising tests don't burn real seconds.

    Tenacity's `_portable_async_sleep` lazy-imports asyncio inside the function and
    calls `asyncio.sleep` at retry-time. Patching `asyncio.sleep` globally for the
    test scope makes those waits instant without disturbing pytest-asyncio internals
    (the event loop schedules via call_soon, not asyncio.sleep). Production code is
    untouched.
    """
    import asyncio as _asyncio

    async def _no_sleep(_seconds: float = 0) -> None:
        return None

    with patch.object(_asyncio, "sleep", new=_no_sleep):
        yield


def _mock_client(response: httpx.Response | Exception, method: str = "post") -> MagicMock:
    """Create a mock httpx.AsyncClient context manager."""
    client = AsyncMock()
    if isinstance(response, Exception):
        getattr(client, method).side_effect = response
    else:
        getattr(client, method).return_value = response
    mock = MagicMock()
    mock.__aenter__ = AsyncMock(return_value=client)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# Generous defaults for tests that don't care about the limits. Individual tests
# override these kwargs when they're exercising the cap.
_TEST_LIMITS: dict[str, int] = {
    "max_zip_bytes": 64 * 1024 * 1024,
    "max_files": 100,
    "max_js_bytes": 1 * 1024 * 1024,
    "max_failed_manifest_bytes": 16 * 1024,
}


async def _download(**overrides: Any) -> BatchResults | None:
    kwargs: dict[str, Any] = {
        "endpoint": "http://pod:10006",
        "auth_token": None,
        **_TEST_LIMITS,
    }
    kwargs.update(overrides)
    return await download_batch(**kwargs)


async def test_submit_batch_success() -> None:
    response = httpx.Response(200, json={"accepted": 2}, request=httpx.Request("POST", "http://pod/generate"))
    mock = _mock_client(response, "post")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await submit_batch(
            endpoint="http://pod:10006",
            prompts=[("p1", "https://cdn/p1.jpg"), ("p2", "https://cdn/p2.jpg")],
            seed=42,
            auth_token="token",
        )

    assert result is True


async def test_submit_batch_timeout() -> None:
    mock = _mock_client(httpx.TimeoutException("timeout"), "post")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await submit_batch(
            endpoint="http://pod:10006",
            prompts=[("p1", "https://cdn/p1.jpg")],
            seed=42,
            auth_token=None,
        )

    assert result is False


async def test_submit_batch_server_error_retries_then_fails() -> None:
    response = httpx.Response(500, request=httpx.Request("POST", "http://pod/generate"))
    mock = _mock_client(httpx.HTTPStatusError("err", request=response.request, response=response), "post")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await submit_batch(
            endpoint="http://pod:10006",
            prompts=[("p1", "https://cdn/p1.jpg")],
            seed=42,
            auth_token=None,
        )

    assert result is False


async def test_submit_batch_409_busy_returns_false() -> None:
    response = httpx.Response(
        409,
        json={"detail": "Cannot accept batch", "current_status": "warming_up"},
        request=httpx.Request("POST", "http://pod/generate"),
    )
    mock = _mock_client(response, "post")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await submit_batch(
            endpoint="http://pod:10006",
            prompts=[("p1", "https://cdn/p1.jpg")],
            seed=42,
            auth_token=None,
        )

    assert result is False


async def test_check_pod_status_generating_with_progress() -> None:
    response = httpx.Response(
        200,
        json={"status": "generating", "progress": 5, "total": 32},
        request=httpx.Request("GET", "http://pod/status"),
    )
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await check_pod_status(
            endpoint="http://pod:10006",
            auth_token=None,
            replacements_remaining=3,
            log_id="test",
        )

    assert result is not None
    assert result.status == PodStatus.GENERATING
    assert result.progress == 5
    assert result.total == 32


async def test_check_pod_status_ready_with_payload() -> None:
    response = httpx.Response(
        200,
        json={"status": "ready", "payload": {"stage": "loading_unet"}},
        request=httpx.Request("GET", "http://pod/status"),
    )
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await check_pod_status(
            endpoint="http://pod:10006",
            auth_token="token",
            replacements_remaining=2,
            log_id="test",
        )

    assert result is not None
    assert result.status == PodStatus.READY
    assert result.payload == {"stage": "loading_unet"}


async def test_check_pod_status_complete() -> None:
    response = httpx.Response(
        200,
        json={"status": "complete"},
        request=httpx.Request("GET", "http://pod/status"),
    )
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await check_pod_status(
            endpoint="http://pod:10006",
            auth_token=None,
            replacements_remaining=1,
            log_id="test",
        )

    assert result is not None
    assert result.status == PodStatus.COMPLETE


async def test_check_pod_status_connection_error() -> None:
    mock = _mock_client(httpx.ConnectError("refused"), "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await check_pod_status(
            endpoint="http://pod:10006",
            auth_token=None,
            replacements_remaining=3,
            log_id="test",
        )

    assert result is None


async def test_check_pod_status_invalid_response() -> None:
    response = httpx.Response(
        200,
        json={"status": "bogus"},
        request=httpx.Request("GET", "http://pod/status"),
    )
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await check_pod_status(
            endpoint="http://pod:10006",
            auth_token=None,
            replacements_remaining=3,
            log_id="test",
        )

    assert result is None


async def test_download_batch_all_success() -> None:
    zip_content = _make_zip({"p1.js": b"js-data-1", "p2.js": b"js-data-2"})
    response = httpx.Response(200, content=zip_content, request=httpx.Request("GET", "http://pod/results"))
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await _download(auth_token="token")

    assert result is not None
    assert result.successes == {"p1": b"js-data-1", "p2": b"js-data-2"}
    assert result.failures == {}


async def test_download_batch_with_failed_manifest() -> None:
    manifest = json.dumps({"p3": "inference timeout", "p4": "image download failed"}).encode()
    zip_content = _make_zip({"p1.js": b"js1", "p2.js": b"js2", "_failed.json": manifest})
    response = httpx.Response(200, content=zip_content, request=httpx.Request("GET", "http://pod/results"))
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await _download()

    assert result is not None
    assert set(result.successes) == {"p1", "p2"}
    assert result.failures == {"p3": "inference timeout", "p4": "image download failed"}


async def test_download_batch_all_failed() -> None:
    manifest = json.dumps({"p1": "timeout"}).encode()
    zip_content = _make_zip({"_failed.json": manifest})
    response = httpx.Response(200, content=zip_content, request=httpx.Request("GET", "http://pod/results"))
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await _download()

    assert result is not None
    assert result.successes == {}
    assert result.failures == {"p1": "timeout"}


async def test_download_batch_empty_archive_returns_none() -> None:
    zip_content = _make_zip({})
    response = httpx.Response(200, content=zip_content, request=httpx.Request("GET", "http://pod/results"))
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await _download()

    assert result is None


async def test_download_batch_strips_directory_prefix() -> None:
    zip_content = _make_zip({"output/p1.js": b"data"})
    response = httpx.Response(200, content=zip_content, request=httpx.Request("GET", "http://pod/results"))
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await _download()

    assert result is not None
    assert "p1" in result.successes


async def test_download_batch_timeout() -> None:
    mock = _mock_client(httpx.TimeoutException("timeout"), "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await _download()

    assert result is None


async def test_download_batch_bad_zip() -> None:
    response = httpx.Response(200, content=b"not-a-zip", request=httpx.Request("GET", "http://pod/results"))
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await _download()

    assert result is None


async def test_download_batch_rejects_oversize_zip() -> None:
    """Raw ZIP bytes exceeding `max_zip_bytes` → reject before parsing."""
    zip_content = _make_zip({"p1.js": b"x" * 100})
    response = httpx.Response(200, content=zip_content, request=httpx.Request("GET", "http://pod/results"))
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await _download(max_zip_bytes=10)  # tiny cap

    assert result is None


async def test_download_batch_rejects_too_many_files() -> None:
    """Archive entry count exceeding `max_files` → reject."""
    zip_content = _make_zip({f"p{i}.js": b"x" for i in range(5)})
    response = httpx.Response(200, content=zip_content, request=httpx.Request("GET", "http://pod/results"))
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await _download(max_files=3)

    assert result is None


async def test_download_batch_rejects_oversize_js() -> None:
    """Any single `.js` entry bigger than `max_js_bytes` → reject."""
    zip_content = _make_zip({"p1.js": b"x" * 100})
    response = httpx.Response(200, content=zip_content, request=httpx.Request("GET", "http://pod/results"))
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await _download(max_js_bytes=50)

    assert result is None


async def test_download_batch_rejects_oversize_failed_manifest() -> None:
    """`_failed.json` bigger than `max_failed_manifest_bytes` → reject."""
    big_manifest = json.dumps({f"p{i}": "r" * 100 for i in range(10)}).encode()
    zip_content = _make_zip({"p1.js": b"ok", "_failed.json": big_manifest})
    response = httpx.Response(200, content=zip_content, request=httpx.Request("GET", "http://pod/results"))
    mock = _mock_client(response, "get")

    with patch("generation_orchestrator.pod_client.httpx.AsyncClient", return_value=mock):
        result = await _download(max_failed_manifest_bytes=50)

    assert result is None

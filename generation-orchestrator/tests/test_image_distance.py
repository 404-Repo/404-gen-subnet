"""Tests for generation_orchestrator.image_distance module."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from generation_orchestrator.image_distance import measure_distance, _measure_distance_with_retry


class TestMeasureDistance:
    """Tests for the image distance measurement functions."""

    @pytest.mark.asyncio
    async def test_successful_measurement(self) -> None:
        """Returns distance on successful response."""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"distance": 0.1234}
        mock_response.raise_for_status = lambda: None

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=mock_response)

        result = await measure_distance(client, "http://svc:8000", "img1.png", "img2.png", "test-1")
        assert result == pytest.approx(0.1234)
        client.post.assert_called_once_with(
            "http://svc:8000/distance",
            json={"url_a": "img1.png", "url_b": "img2.png"},
        )

    @pytest.mark.asyncio
    async def test_returns_none_when_distance_is_null(self) -> None:
        """Service returns null distance (e.g. file not found) -> None, no retry."""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"distance": None}
        mock_response.raise_for_status = lambda: None

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=mock_response)

        result = await measure_distance(client, "http://svc:8000", "a.png", "b.png", "test-2")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_after_retries_exhausted(self) -> None:
        """After 3 retries on HTTPStatusError, returns None."""
        mock_response = AsyncMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        mock_response.json.return_value = {}
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "server error", request=httpx.Request("POST", "http://svc"), response=mock_response
        )

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=mock_response)

        # Patch retry to avoid real delays
        result = await measure_distance(client, "http://svc:8000", "a.png", "b.png", "test-3")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_unexpected_exception(self) -> None:
        """Non-retryable exceptions are caught and return None."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=ValueError("unexpected"))

        result = await measure_distance(client, "http://svc:8000", "a.png", "b.png", "test-4")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_float_type(self) -> None:
        """Ensure returned distance is always a float."""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"distance": 5}  # int from JSON
        mock_response.raise_for_status = lambda: None

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=mock_response)

        result = await measure_distance(client, "http://svc:8000", "a.png", "b.png", "test-5")
        assert isinstance(result, float)
        assert result == 5.0

    @pytest.mark.asyncio
    async def test_timeout_exception_returns_none(self) -> None:
        """httpx.TimeoutException should be retried and ultimately return None."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

        result = await measure_distance(client, "http://svc:8000", "a.png", "b.png", "test-6")
        assert result is None

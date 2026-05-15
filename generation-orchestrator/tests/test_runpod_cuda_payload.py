"""Runpod deploy payload includes allowedCudaVersions when configured."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from generation_orchestrator.gpu_provider.runpod_client import RunpodClient


@pytest.mark.asyncio
async def test_deploy_container_sets_allowed_cuda_versions() -> None:
    captured: dict = {}

    async def fake_request(*args: object, **kwargs: object) -> MagicMock:
        j = kwargs.get("json")
        if isinstance(j, dict) and j.get("name") == "test-pod":
            captured.update(j)
        resp = MagicMock()
        resp.json.return_value = {"id": "pod-1", "name": "n"}
        resp.raise_for_status = MagicMock()
        return resp

    client = RunpodClient(api_key="k")
    client._http_client = MagicMock()
    client._http_client.request = AsyncMock(side_effect=fake_request)

    await client.deploy_container(
        "test-pod",
        image="img:latest",
        gpu_type="H200",
        gpu_count=4,
        port=10006,
        allowed_cuda_versions=["13.0", "12.9"],
    )

    assert captured.get("allowedCudaVersions") == ["13.0", "12.9"]
    assert "gpuTypeIds" in captured

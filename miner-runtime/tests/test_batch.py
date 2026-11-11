"""Batch limits and blocking renderer transport."""

import asyncio
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from bittensor_wallet import Keypair
from witness_client import AsyncWitnessClient

from miner_runtime import batch
from miner_runtime.runner import _AsyncCaptureSource


@pytest.mark.parametrize("preparations", [0, 6])
def test_batch_uses_sdk_preparation_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preparations: int,
) -> None:
    config = tmp_path / "witness.json"
    config.write_text(json.dumps({"witness_url": "https://witness.test",
                                  "proof_parameters": str(tmp_path / "parameters")}))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setattr(batch, "Wallet", lambda **kwargs: SimpleNamespace(
        hotkey=Keypair.create_from_uri("//Alice")))
    saved: list[bytes] = []

    async def prover(client: AsyncWitnessClient) -> None:
        pass

    async def submitted(http: httpx.AsyncClient) -> set[str]:
        return set()

    async def submit(client: AsyncWitnessClient, image: bytes, source: str | None) -> dict:
        saved.append(image)
        return {"saved": True}

    async def generate(image: bytes, seed: int, witness: AsyncWitnessClient) -> str:
        return "export default {};"

    monkeypatch.setattr(AsyncWitnessClient, "_get_prover", prover)
    monkeypatch.setattr(AsyncWitnessClient, "submit", submit)
    operation = batch.run(generate, {"prompt": b"image"}, 0, witness_config=config,
                          read_saved=submitted, parallel_preparations=preparations, attempts=1, api=True)
    if preparations == 0:
        with pytest.raises(RuntimeError, match="1 prompts remain"):
            asyncio.run(operation)
        assert saved == []
    else:
        asyncio.run(operation)
        assert saved == [b"image"]


def test_batch_with_an_initialized_client_needs_no_config_or_wallet(monkeypatch):
    saved = []

    def wallet(**kwargs):
        raise AssertionError("The initialized client already owns its identity")

    class Witness:
        async def submit(self, image, source):
            saved.append((image, source))

    async def submitted(http):
        return set()

    async def generate(image, seed, witness):
        return image.decode() + str(seed)

    monkeypatch.setattr(batch, "Wallet", wallet)
    asyncio.run(batch.run_pass(generate, {"prompt": b"image"}, 42, client=Witness(), read_saved=submitted))
    assert saved == [(b"image", "image42")]


@pytest.mark.parametrize("compressed", [False, True])
def test_blocking_renderer_bridge_preserves_decoded_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compressed: bool,
) -> None:
    expected = b"rendered png"

    async def scenario() -> None:
        witness = AsyncWitnessClient(
            {"witness_url": "https://witness.test", "proof_parameters": str(tmp_path)},
            hotkey=Keypair.create_from_uri("//Alice"), openai_api_key="synthetic",
        )

        async def send(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, request=request,
                headers={"Content-Encoding": "gzip"} if compressed else {},
                content=gzip.compress(expected) if compressed else expected,
            )

        monkeypatch.setattr(witness._gateway, "send", send)
        source = _AsyncCaptureSource(witness, asyncio.get_running_loop())

        def render() -> bytes:
            with httpx.Client(transport=source.renderer_transport()) as client:
                return client.post("https://witness.internal/v1/render/grid").content

        try:
            assert await asyncio.to_thread(render) == expected
        finally:
            await witness.aclose()

    asyncio.run(scenario())

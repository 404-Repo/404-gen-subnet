"""Batch configuration uses the SDK's preparation limit contract."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from bittensor_wallet import Keypair
import pytest
from witness_client import AsyncWitnessClient

from miner_runtime import batch


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

    async def submitted(client: AsyncWitnessClient) -> dict:
        return {}

    async def submit(client: AsyncWitnessClient, image: bytes, source: str | None) -> dict:
        saved.append(image)
        return {"saved": True}

    async def generate(image: bytes, seed: int, witness: AsyncWitnessClient) -> str:
        return "export default {};"

    monkeypatch.setattr(AsyncWitnessClient, "_get_prover", prover)
    monkeypatch.setattr(AsyncWitnessClient, "get_submitted", submitted)
    monkeypatch.setattr(AsyncWitnessClient, "submit", submit)
    operation = batch.run(generate, {"prompt": b"image"}, 0, witness_config=config,
                          parallel_preparations=preparations, attempts=1, api=True)
    if preparations == 0:
        with pytest.raises(RuntimeError, match="1 prompts remain"):
            asyncio.run(operation)
        assert saved == []
    else:
        asyncio.run(operation)
        assert saved == [b"image"]

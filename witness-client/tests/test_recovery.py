"""Recovery failures must keep the same exchange available for another attempt."""

import json
import time
import uuid
from pathlib import Path

import httpx
import pytest
from bittensor_wallet import Keypair

from witness_client import WitnessError
from witness_client.gateway import GatewayClient

def test_sealed_capture_never_triggers_post_generation_claim(
    tmp_path: Path,
) -> None:
    identifier = str(uuid.uuid4())
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    directory = tmp_path / identifier
    directory.mkdir()
    (directory / "job.json").write_text(json.dumps({"deadline": time.time() + 60}))
    polls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "A sealed capture must not upload a proof"
        polls.append(request.url.path)
        return httpx.Response(200, json={"status": "sealed"})

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(handler)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 60)
        with pytest.raises(WitnessError, match="sealed") as error:
            gateway.wait_for_evidence(identifier)
    assert error.value.exchange_id == identifier
    assert polls == [f"/v1/exchanges/{identifier}/progress"]

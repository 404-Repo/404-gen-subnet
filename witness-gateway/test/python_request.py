"""Emit one hotkey-signed request for the workerd interoperability test."""

import json
import sys
import tempfile
from pathlib import Path
from typing import cast

import httpx
from bittensor_wallet import Keypair

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "witness-server/worker"))
from identity import Provider, authenticate_exchange

from client import QueueClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "witness-client/src"))
from witness_client.gateway import GatewayClient

key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
provider = cast(Provider, sys.argv[3])
if sys.argv[2] == "queue":
    with QueueClient(sys.argv[1], key) as client:
        body = json.dumps(client.prepare(provider), separators=(",", ":")).encode()
        headers = client.signed_request("POST", "/run", body)
else:
    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    **json.loads(request.content),
                    "queue_state": "queued",
                    "round": "1",
                    "session": None,
                },
            )
        return httpx.Response(
            200,
            json={
                **json.loads(requests[0].content),
                "queue_state": "connected",
                "round": "1",
                "session": {
                    "id": json.loads(requests[0].content)["exchange_id"],
                    "protocol": 2,
                    "provider": "https://api.openai.com/v1/responses",
                    "proof_key_sha256": "a" * 64,
                    "round": "1",
                    "miner": key.ss58_address,
                },
            },
        )

    with tempfile.TemporaryDirectory() as directory, httpx.Client(
        base_url=sys.argv[1], transport=httpx.MockTransport(capture)
    ) as http:
        GatewayClient(http, key, Path(directory), 5).queue_and_wait_for_worker(provider)
    body, headers = requests[0].content, dict(requests[0].headers)

job = json.loads(body)
assert "round" not in job
assert authenticate_exchange({**job, "round": "1"}, "1") == (
    key.ss58_address,
    job["exchange_id"],
    provider,
)
print(
    json.dumps({"hotkey": key.ss58_address, "body": body.decode(), "headers": headers})
)

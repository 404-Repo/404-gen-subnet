"""Real Python gateway transports with fixture bytes, without provider inference."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

import httpx
from bittensor_wallet import Keypair
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "witness-client/src"))
sys.path.insert(0, str(ROOT / "witness-server/worker"))

from channel import WorkerChannel
from identity import authenticate_exchange, worker_connection
from witness_client import WitnessClient
from witness_client.gateway import GatewayClient, encode

PARAMS = ROOT / "witness-server/verifier/params"
TRAFFIC = bytes(range(256)) * 1024


def worker(data: dict[str, object]) -> None:
    hotkey, identifier, provider = authenticate_exchange(data, "test-0")
    url, token, _ = worker_connection(data)
    host = (
        "api.openai.com/v1/responses"
        if provider == "api"
        else "chatgpt.com/backend-api/codex/responses"
    )
    with connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"},
        proxy=None,
        compression=None,
        max_size=2 * 1024 * 1024,
    ) as socket:
        channel = WorkerChannel(socket)
        socket.send(
            encode(
                {
                    "type": "ready",
                    "session": {
                        "id": identifier,
                        "protocol": 2,
                        "provider": f"https://{host}",
                        "proof_key_sha256": hashlib.sha256(
                            (PARAMS / "verifying.bin").read_bytes()
                        ).hexdigest(),
                        "round": "test-0",
                        "miner": hotkey,
                    },
                }
            ).decode()
        )
        assert channel.recv(time.monotonic() + 30) == TRAFFIC
        channel.send(TRAFFIC[::-1])
        for phase in ("handshake", "authorize"):
            try:
                channel.recv(time.monotonic() + 30)
            except TimeoutError:
                pass
            assert json.loads(channel.claim(phase) or b"null") == {"fixture": phase}
            channel.notify(
                {
                    "status": "running",
                    "handshook": True,
                    "authorized": phase == "authorize",
                }
            )
        channel.finish()


def miner(data: dict[str, object]) -> None:
    # Wallet 4.1 accepts a hex string; its stub incorrectly declares bytes.
    key = Keypair.create_from_seed(str(data["seed"]))  # type: ignore[arg-type]
    with (
        tempfile.TemporaryDirectory() as directory,
        httpx.Client(base_url=str(data["origin"])) as http,
    ):
        state = Path(directory)
        gateway = GatewayClient(http, key, state, 20)
        session = gateway.queue_and_wait_for_worker("api")
        assert session["round"] == "test-0"
        identifier = session["id"]
        with gateway.tunnel(identifier) as tunnel:
            tunnel.send(TRAFFIC)
            assert tunnel.recv(10) == TRAFFIC[::-1]
            for phase in ("handshake", "authorize"):
                status = tunnel.verify(phase, encode({"fixture": phase}), 10)
                assert status["handshook"]
            assert tunnel.recv(10) == b""
        # Retained evidence is a fixture; exercise the unchanged public recovery API.
        (state / identifier / "session.json").write_bytes(encode(session))
        (state / identifier / "claim.json").write_bytes(b"{}")
        with WitnessClient(
            {
                "witness_url": str(data["origin"]),
                "state_directory": directory,
                "proof_parameters": str(PARAMS),
            },
            hotkey=key,
            openai_api_key="fixture",
        ) as client:
            result = client.resume(identifier)
            assert result.output_text == "recorded fixture"
            assert result.usage["total_tokens"] == 4
            assert client.resume(identifier).output_text == result.output_text
            client.release(identifier)


data = cast(dict[str, object], json.load(sys.stdin))
worker(data) if sys.argv[1] == "worker" else miner(data)
print("contract passed")

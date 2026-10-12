"""Recovery failures must keep the same exchange available for another attempt."""

import hashlib
import json
import time
import uuid
from pathlib import Path

import httpx
import pytest
from bittensor_wallet import Keypair

from witness_client import WitnessClient, WitnessConfig, WitnessError, recovery
from witness_client.gateway import GatewayClient
from witness_client.transport import MAX_BYTES

PARAMS = Path(__file__).resolve().parents[2] / "witness-server/verifier/params"


@pytest.mark.parametrize("failure", ["unavailable", "oversized", "disconnect"])
def test_response_download_failure_preserves_recovery_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    hotkey = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = str(uuid.uuid4())
    config: WitnessConfig = {
        "witness_url": "https://localhost",
        "proof_parameters": str(PARAMS),
        "state_directory": str(tmp_path / "state"),
    }
    downloads = 0
    clock = 0.0
    monkeypatch.setattr("witness_client.delivery.time.monotonic", lambda: clock)
    status_path = f"/exchanges/{identifier}/progress"
    response_path = f"/exchanges/{identifier}/response.sse"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal downloads, clock
        assert (
            request.method == "GET"
        )  # Recovery must not allocate or authorize another call.
        if request.url.path == status_path:
            return httpx.Response(
                200, json={"status": "issued", "evidence_expires": 9_999_999_999_999}
            )
        assert request.url.path == response_path
        downloads += 1
        if downloads == 1:
            clock += (
                1  # Exhaust the delivery window so queue retries surface the failure.
            )
            if failure == "unavailable":
                return httpx.Response(503, json={"error": "temporarily_unavailable"})
            if failure == "disconnect":
                raise httpx.ReadError("Response connection dropped", request=request)
            return httpx.Response(200, content=b"x" * (MAX_BYTES + 1))
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {"type": "output_text", "text": "recovered"}
                                ],
                            }
                        ],
                        "usage": {"total_tokens": 4},
                    },
                }
            ).encode()
            + b"\n\n",
        )

    with WitnessClient(
        config, openai_api_key="x" * 80, hotkey=hotkey, delivery_timeout=1
    ) as client:
        directory = client._state / identifier
        directory.mkdir(mode=0o700)
        (directory / "session.json").write_text(
            json.dumps(
                {
                    "protocol": 2,
                    "id": identifier,
                    "provider": "https://api.openai.com/v1/responses",
                    "round": "1",
                    "miner": hotkey.ss58_address,
                    "proof_key_sha256": hashlib.sha256(
                        (PARAMS / "verifying.bin").read_bytes()
                    ).hexdigest(),
                }
            )
        )
        (directory / "claim.json").write_text("{}")
        client._http.close()
        client._http = httpx.Client(
            base_url=client._http.base_url, transport=httpx.MockTransport(handler)
        )
        client._gateway.http = client._http
        with pytest.raises(WitnessError) as error:
            client.resume(identifier)
        assert error.value.exchange_id == identifier
        assert error.value.status_code == (503 if failure == "unavailable" else None)
        assert error.value.code == (
            "temporarily_unavailable" if failure == "unavailable" else None
        )
        if failure == "disconnect":
            assert isinstance(error.value.__cause__, httpx.ReadError)
        assert not (directory / "response.bin").exists()
        result = client.resume(error.value.exchange_id)
        assert result.output_text == "recovered" and result.usage == {"total_tokens": 4}
        assert downloads == 2


@pytest.mark.parametrize("protocol", [None, 1, 3])
@pytest.mark.parametrize("has_claim", [False, True])
def test_recovery_rejects_unsupported_protocol_without_touching_old_files(
    tmp_path: Path,
    protocol: int | None,
    has_claim: bool,
) -> None:
    directory = tmp_path / str(uuid.uuid4())
    directory.mkdir()
    session = {} if protocol is None else {"protocol": protocol}
    files = {
        "session.json": json.dumps(session).encode(),
        "private.json": b"old private records",
        "disclosures.json": b"old disclosures",
    }
    if has_claim:
        files["claim.json"] = b"old prepared claim"
    for name, body in files.items():
        (directory / name).write_bytes(body)
    with pytest.raises(WitnessError) as error:
        recovery.load_session(directory)
    assert error.value.code == "unsupported_protocol"
    assert error.value.exchange_id == directory.name
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == files


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
    assert polls == [f"/exchanges/{identifier}/progress"]

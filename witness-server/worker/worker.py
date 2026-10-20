"""One queued job authorizes, captures and records one OpenAI exchange."""

from __future__ import annotations

import hashlib
import json
import os
import select
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import cast

from channel import Channel, WorkerChannel
from identity import (
    WITNESS_ID,
    Provider,
    authenticate_exchange,
    miner_prefix,
    validate_round,
    worker_connection,
)
from storage import MAX_BYTES, R2Store, Store, json_bytes
from timing import emit, identify_exchange, measure, snapshot, timed
from timing import exchange_id as _exchange_id
from timing import forward as _verifier_timings
from timing import started as timing_started
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

HERE = Path(__file__).resolve().parent
WITNESS = HERE.parent / "verifier"
PARAMETERS = Path(os.environ.get("WITNESS_PARAMETERS", str(WITNESS / "params")))
CLIENT_HELLO_TIMEOUT = 60
AUTHORIZATION_TIMEOUT = 300
PROVIDERS: dict[Provider, tuple[str, str]] = {
    "codex": ("chatgpt.com", "/backend-api/codex/responses"),
    "api": ("api.openai.com", "/v1/responses"),
}
Verifier = Callable[[str, Path], dict[str, object]]


def complete_records(data: bytes | bytearray) -> bool:
    offset = 0
    while offset < len(data):
        if len(data) - offset < 5:
            return False
        offset += 5 + int.from_bytes(data[offset + 3 : offset + 5], "big")
    return offset == len(data)


def verify(
    mode: str, directory: Path, *, provider: Provider | None = None
) -> dict[str, object]:
    with measure("verifier_subprocess"):
        result = subprocess.run(
            [
                "node",
                str(HERE / "verify.mjs"),
                mode,
                str(directory),
                str(PARAMETERS),
                *([provider] if provider is not None else []),
            ],
            capture_output=True,
            timeout=180,
            check=False,
            env={
                **os.environ,
                "NODE_OPTIONS": "--max-old-space-size=384",
                "WITNESS_EXCHANGE_ID": _exchange_id(),
                "WITNESS_VERIFIER_MODE": mode,
            },
        )
    _verifier_timings(result.stderr)
    if result.returncode:
        raise ValueError("Witness verification rejected the exchange")
    return cast(dict[str, object], json.loads(result.stdout))


@timed("relay")
def relay(
    store: Store,
    prefix: str,
    deadline: float,
    directory: Path,
    verifier: Verifier,
    channel: Channel,
    *,
    round_number: str,
    hotkey: str,
    host: str,
    port: int = 443,
    before_release: Callable[[bytes], None] | None = None,
    extra_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """Finish TLS promptly, then hold HTTP until a durable authorization exists.

    After release, provider capture is independent of the miner. The complete
    response and its usage become durable before its ciphertext is delivered.
    """
    if prefix.rsplit("/", 1)[0] != miner_prefix(round_number, hotkey):
        raise ValueError("Transcript path differs from authenticated identity")
    identify_exchange(prefix.rsplit("/", 1)[1])
    started = time.monotonic()
    hello_deadline = min(deadline, started + CLIENT_HELLO_TIMEOUT)
    authorization_deadline = min(deadline, started + AUTHORIZATION_TIMEOUT)
    client = bytearray()
    server = bytearray()
    issued = False
    handshake = False
    handshake_bytes = 0
    try:
        with measure("provider_tcp_connect"):
            connection = socket.create_connection((host, port), timeout=10)
        with connection as provider:
            # Read one whole TLS record, then let the shared gate validate it.
            with measure("client_hello_wait"):
                while len(client) < 5 or len(client) < 5 + int.from_bytes(
                    client[3:5], "big"
                ):
                    if time.monotonic() >= hello_deadline:
                        raise TimeoutError("ClientHello deadline reached")
                    chunk = channel.recv(hello_deadline)
                    if not chunk:
                        raise ValueError("Missing ClientHello")
                    client.extend(chunk)
                    if (
                        len(client) >= 5
                        and 5 + int.from_bytes(client[3:5], "big") > 16645
                    ):
                        raise ValueError("Oversized ClientHello")
            (directory / "client.bin").write_bytes(client)
            with measure("verify_hello"):
                gate = verifier("hello", directory)
            released = int(str(gate["released"]))
            provider.sendall(client[:released])

            handshake_started = timing_started()
            authorization_started = handshake_started
            # The server's first TLS flight is safe to deliver. Verify and
            # release Finished first, then verify and release the HTTP request.
            while time.monotonic() < authorization_deadline:
                if select.select([provider], [], [], 0)[0]:
                    chunk = provider.recv(65536)
                    if not chunk:
                        raise ValueError("Provider closed before authorization")
                    server.extend(chunk)
                    if len(server) > 128 * 1024:
                        raise ValueError("Server handshake exceeds limit")
                    # Before request authorization the shared verifier permits
                    # only TLS setup and its fixed, unauthenticated HEAD probe.
                    channel.send(chunk)
                try:
                    chunk = channel.recv(
                        min(authorization_deadline, time.monotonic() + 0.05)
                    )
                    if not chunk:
                        raise ValueError("Miner left before authorization")
                    client.extend(chunk)
                    if len(client) > MAX_BYTES:
                        raise ValueError("Client traffic exceeds limit")
                except TimeoutError:
                    pass
                claim_name = "claim.json" if handshake else "handshake-claim.json"
                raw_claim = channel.claim("authorize" if handshake else "handshake")
                if raw_claim is None:
                    continue
                claim = json.loads(raw_claim)
                size = claim.get("client_bytes")
                if (
                    not isinstance(size, int)
                    or not 0 < size <= MAX_BYTES
                    or len(client) > size
                ):
                    raise ValueError("Invalid authorized request size")
                if len(client) < size or not complete_records(server):
                    continue
                if handshake and len(server) == handshake_bytes:
                    continue  # Wait for a ticket to confirm the committed key.
                (directory / "client.bin").write_bytes(client)
                (directory / "server.bin").write_bytes(server)
                (directory / claim_name).write_bytes(raw_claim)
                if not handshake:
                    emit("handshake_capture_wait", handshake_started)
                    with measure("verify_handshake"):
                        verifier("handshake", directory)
                    with measure("handshake_persistence"):
                        store.put(
                            prefix + "/handshake.json",
                            (directory / "handshake.json").read_bytes(),
                        )
                    handshake_bytes = len(server)
                    handshake = True
                    channel.notify(
                        {"status": "running", "handshook": True, "authorized": False}
                    )
                    provider.sendall(client[released:])
                    released = len(client)
                    authorization_started = timing_started()
                    continue
                emit("authorization_claim_wait", authorization_started)
                with measure("verify_authorize"):
                    verifier("authorize", directory)
                # These writes complete before any billable HTTP bytes leave.
                with measure("authorization_persistence"):
                    for name in (
                        "client.bin",
                        "server.bin",
                        "claim.json",
                        "authorization.json",
                        "request.json",
                    ):
                        store.put(prefix + "/" + name, (directory / name).read_bytes())
                    store.put(
                        prefix + "/status.json",
                        json_bytes(
                            {"status": "running", "handshook": True, "authorized": True}
                        ),
                    )
                if time.monotonic() >= authorization_deadline:
                    raise TimeoutError("Request authorization deadline reached")
                if before_release is not None:
                    (directory / "authorization-server.bin").write_bytes(server)
                    store.put(prefix + "/authorization-server.bin", bytes(server))
                    before_release((directory / "request.json").read_bytes())
                    if time.monotonic() >= authorization_deadline:
                        raise TimeoutError("Request authorization deadline reached")
                channel.notify(
                    {"status": "running", "handshook": True, "authorized": True}
                )
                handshake_bytes = len(server)
                with measure("provider_request_release"):
                    provider.sendall(client[released:])
                    break
            else:
                raise TimeoutError("Request authorization deadline reached")

            response_started = timing_started()
            first_response = True
            # Do not consume client messages or let its disconnect cancel GPT.
            with measure("provider_response_capture"):
                while time.monotonic() < deadline:
                    if not select.select([provider], [], [], 0.2)[0]:
                        continue
                    chunk = provider.recv(65536)
                    if not chunk:
                        break
                    if first_response:
                        emit("provider_first_ciphertext_wait", response_started)
                        first_response = False
                    server.extend(chunk)
                    if len(server) > MAX_BYTES:
                        raise ValueError("Provider traffic exceeds 16 MiB")
                else:
                    raise TimeoutError("Provider exchange deadline reached")
        (directory / "server.bin").write_bytes(server)
        with measure("final_transcript_persistence"):
            store.put(prefix + "/server.bin", bytes(server))
        with measure("verify_finish"):
            metadata = verifier("finish", directory)
        metadata.update(
            {
                "verified_at": time.time(),
                "client_bytes": len(client),
                "server_bytes": len(server),
                "prefix": prefix,
                "round": round_number,
                "hotkey": hotkey,
                "exchange_id": prefix.rsplit("/", 1)[1],
            }
        )
        if extra_metadata is not None:
            metadata.update(extra_metadata)
        with measure("evidence_persistence"):
            for name in ("request.json", "response.sse"):
                store.put(prefix + "/" + name, (directory / name).read_bytes())
            with measure("manifest_persistence"):
                store.put(prefix + "/manifest.json", json_bytes(metadata))
            with measure("final_status_persistence"):
                store.put(
                    prefix + "/status.json",
                    json_bytes(
                        {
                            "status": "issued",
                            "authorized": True,
                            "usage": metadata.get("usage"),
                            "provider_status": metadata["provider_status"],
                            "usage_state": (
                                "rejected"
                                if metadata["provider_rejected"]
                                else (
                                    "recorded"
                                    if metadata.get("usage") is not None
                                    else "unknown"
                                )
                            ),
                            "evidence_expires": int((time.time() + 7 * 86400) * 1000),
                        }
                    ),
                )
        issued = True
        # Capture is durable before delivery. A disconnected miner can recover it.
        with measure("delivery"), suppress(OSError, WebSocketException):
            channel.send(bytes(server[handshake_bytes:]))
            channel.finish()
        return metadata
    finally:
        # Ordinary Pods retain the spool for upload/finalization recovery. This
        # never opens another provider connection or repeats a paid request.
        with suppress(OSError):
            (directory / "client.bin").write_bytes(client)
            (directory / "server.bin").write_bytes(server)
        if not issued:
            with suppress(OSError, WebSocketException):
                channel.finish()


@timed("job")
def handler(job: dict[str, object]) -> dict[str, object]:
    data = cast(dict[str, object], job["input"])
    with measure("verifying_key_hash"):
        proof_hash = hashlib.sha256(
            (PARAMETERS / "verifying.bin").read_bytes()
        ).hexdigest()
    if data.get("operation") == "health":
        validate_round(os.environ["WITNESS_ROUND"])
        store = R2Store()
        key = f"experiments/health-{uuid.uuid4()}/check"
        try:
            store.put(key, b"ready")
            if store.get(key) != b"ready":
                raise RuntimeError("R2 read-back failed")
        finally:
            store.delete(key)
        verify("health", PARAMETERS)
        return {
            "status": "ready",
            "proof_key_sha256": proof_hash,
            "r2": "read-write-ok",
            "round": os.environ["WITNESS_ROUND"],
            "receiver": WITNESS_ID,
        }
    if data.get("operation") != "exchange":
        raise ValueError("Unknown operation")
    round_number = os.environ["WITNESS_ROUND"]
    with measure("authenticate_job"):
        hotkey, identifier, provider = authenticate_exchange(data, round_number)
    identify_exchange(identifier)
    host, path = PROVIDERS[provider]
    gateway_url, token, expires = worker_connection(data)
    deadline = time.monotonic() + expires - time.time()
    prefix = f"{miner_prefix(round_number, hotkey)}/{identifier}"
    store = R2Store()
    session = {
        "id": identifier,
        "protocol": 2,
        "provider": f"https://{host}{path}",
        "proof_key_sha256": proof_hash,
        "round": round_number,
        "miner": hotkey,
    }
    # Claim before opening the provider connection or writing evidence. A duplicate
    # RunPod delivery cannot reuse the private, single-use gateway capability.
    with measure("gateway_websocket_connect"):
        socket = connect(
            gateway_url,
            additional_headers={"Authorization": f"Bearer {token}"},
            proxy=None,
            compression=None,
            max_size=2 * 1024 * 1024,
            max_queue=16,
            open_timeout=30,
            close_timeout=5,
        )
    with socket:
        channel = WorkerChannel(socket)
        try:
            with measure("session_persistence_and_ready"):
                store.put(
                    prefix + "/status.json",
                    json_bytes({"status": "running", "authorized": False}),
                )
                store.put(prefix + "/session.json", json_bytes(session))
                socket.send(json_bytes({"type": "ready", "session": session}).decode())
            with tempfile.TemporaryDirectory(prefix="witness-") as temporary:
                result = relay(
                    store,
                    prefix,
                    deadline,
                    Path(temporary),
                    partial(verify, provider=provider),
                    channel,
                    round_number=round_number,
                    hotkey=hotkey,
                    host=host,
                )
        except Exception as error:  # noqa: BLE001
            # Expose the exception type without provider or credential details.
            store.put(
                prefix + "/status.json",
                json_bytes({"status": "failed", "error_type": type(error).__name__}),
            )
            raise RuntimeError(
                f"Exchange {identifier} failed ({type(error).__name__})"
            ) from None

    result["timings"] = snapshot()
    return result


if __name__ == "__main__":
    import runpod

    runpod.serverless.start({"handler": handler})

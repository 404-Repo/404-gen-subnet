"""Native proofs and provider TLS through the actual direct Pod service, offline."""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import openai
import pytest
from channel import Channel
from storage import Store
from test_service import Harness, exchange_id
from witness_client import WitnessClient
from witness_client.calls import RequestKey
from witness_client.request import serialize_body
from witness_client.transport import exchange
from worker import Verifier, relay

from pod_gateway.certs import create


@pytest.mark.parametrize("leave,status", [(False, 200), (True, 200), (False, 401)])
def test_native_direct_tls_durable_completion_and_exact_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leave: bool,
    status: int,
) -> None:
    provider_dir = tmp_path / "provider"
    create(provider_dir, "localhost")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(provider_dir / "gateway.pem", provider_dir / "gateway.key")
    root = Path(__file__).resolve().parents[2]
    done = threading.Event()
    text = "native fixture " + "x" * (1024 * 1024)
    response_body = (
        b"data: "
        + json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "fixture",
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                    "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                },
            }
        ).encode()
        + b"\n\n"
        if status == 200
        else b'{"error":{"message":"Fixture rejection"}}'
    )
    response = (
        f"HTTP/1.1 {status} Fixture\r\nContent-Length: {len(response_body)}\r\nConnection: close\r\n\r\n".encode()
        + response_body
    )
    seen: list[bytes] = []

    with socket.socket() as listener, ThreadPoolExecutor(max_workers=1) as pool:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(60)

        def provider() -> None:
            with context.wrap_socket(listener.accept()[0], server_side=True) as connection:
                connection.settimeout(60)
                probe = bytearray()
                while b"\r\n\r\n" not in probe:
                    probe.extend(connection.recv(65536))
                assert probe == b"HEAD /v1/responses HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n"
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: keep-alive\r\n\r\n")
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    request.extend(connection.recv(65536))
                head, body = bytes(request).split(b"\r\n\r\n", 1)
                size = int(
                    next(line for line in head.split(b"\r\n") if line.startswith(b"Content-Length:")).split(b":")[1]
                )
                while len(body) < size:
                    body += connection.recv(65536)
                seen.append(body)
                connection.sendall(response)
            done.set()

        captured = pool.submit(provider)

        def verify(mode: str, directory: Path, *, provider: str) -> dict[str, object]:
            (directory / "ca.pem").write_bytes((provider_dir / "ca.pem").read_bytes())
            result = subprocess.run(
                [
                    "node",
                    str(root / "witness-server/worker/tests/verify-fixture.mjs"),
                    mode,
                    str(directory),
                    str(root / "witness-server/verifier/params"),
                    provider,
                ],
                capture_output=True,
                timeout=180,
                check=False,
            )
            assert result.returncode == 0, result.stderr.decode()
            return cast(dict[str, object], json.loads(result.stdout))

        def direct_relay(
            store: Store,
            prefix: str,
            deadline: float,
            directory: Path,
            verifier: Verifier,
            channel: Channel,
            **kwargs: object,
        ) -> dict[str, object]:
            kwargs.pop("host")
            return relay(
                store,
                prefix,
                deadline,
                directory,
                verifier,
                channel,
                host="127.0.0.1",
                port=listener.getsockname()[1],
                **kwargs,  # type: ignore[arg-type]
            )

        async def scenario() -> None:
            fixture = Harness(tmp_path, monkeypatch, native=True)
            monkeypatch.setattr("pod.relay", direct_relay)
            monkeypatch.setattr("pod.verify", verify)
            monkeypatch.setitem(
                __import__("witness_client.request", fromlist=["PROVIDERS"]).PROVIDERS,
                "api",
                ("localhost", "/v1/responses"),
            )

            def client_exchange(*args: object, **kwargs: object) -> bytes:
                return exchange(*args, ca_file=provider_dir / "ca.pem", **kwargs)  # type: ignore[arg-type]

            monkeypatch.setattr("witness_client.client.exchange", client_exchange)
            original = WitnessClient._authorize
            if leave:

                def authorize(client: WitnessClient, tunnel: object, disclosure: object) -> None:
                    original(client, tunnel, disclosure)  # type: ignore[arg-type]
                    tunnel.close()  # type: ignore[attr-defined]
                    raise OSError("Fixture miner left after authorization")

                monkeypatch.setattr(WitnessClient, "_authorize", authorize)
            async with fixture.running():
                with fixture.client() as witness:
                    gateway = witness._gateway
                    await asyncio.to_thread(gateway.ensure_pod)
                    await fixture.runners[0].cleanup()
                    gateway_bytes = fixture.gateway_bytes
                    bound = witness

                    def run() -> None:
                        if status != 200:
                            with pytest.raises(openai.APIStatusError) as rejected:
                                bound.create(model="fixture", input="x" * (1024 * 1024))
                            assert rejected.value.status_code == status
                            return
                        answer = bound.create(model="fixture", input="x" * (1024 * 1024))
                        assert answer.output_text == text
                        if not leave:
                            assert fixture.origin_bytes == 0
                        assert witness.resume(exchange_id(witness)).output_text == text

                    await asyncio.wait_for(asyncio.to_thread(run), 120)
                    assert done.is_set() and len(seen) == 1
                    assert fixture.gateway_bytes == gateway_bytes
                    assert fixture.provider.creates == 1
                    key = RequestKey.from_body("api", serialize_body({"model": "fixture", "input": "x" * (1024 * 1024),
                                                                    "stream": True, "service_tier": "default"}, "api"))
                    recording = fixture.records.load(fixture.scope, key.id)
                    assert recording is not None
                    call = recording.call
                    assert call.phase == "issued"
                    assert (
                        fixture.state.get(f"rounds/test-0/{fixture.scope.hotkey}/{call.exchange_id}/response.sse")
                        == response_body
                    )
                    summary = fixture.runtimes[0].budget.summary(
                        fixture.scope, fixture.records.locate(fixture.scope, call.exchange_id)[0]  # type: ignore[index]
                    )
                    assert summary["possible_dispatches"] == 1
                    assert summary["pending_usage"] == (0 if status == 200 else 1)

        asyncio.run(scenario())
        captured.result(timeout=5)

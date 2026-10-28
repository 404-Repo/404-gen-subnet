"""Native proofs, provider TLS and offline validator replay of synthetic outcomes."""

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
from miner_runtime.runner import run_generation
from miner_runtime.validator import ReplayWitness, run_replay
from openai.types.responses import Response
from storage import Store
from test_recording import PRICING
from test_service import Harness
from witness_client import WitnessClient
from witness_client.response import ModelStream
from witness_client.transport import exchange
from worker import Verifier, relay

from pod_gateway.certs import create
from pod_gateway.replay import export_bundle


def outcome_body(outcome: str) -> tuple[int, bytes]:
    if outcome == "http-error":
        return 429, b'{"error":{"message":"Synthetic rate limit","code":"rate_limit_exceeded","param":"model"}}'
    first = {"type": "response.created", "response": {"id": "fixture", "status": "in_progress"}}
    if outcome == "sse-error":
        events = [first, {"type": "error", "code": "fixture_error", "message": "Synthetic failure"}]
    elif outcome == "partial":
        events = [
            first,
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "partial"},
        ]
    else:
        status = outcome if outcome in ("failed", "incomplete") else "completed"
        content = (
            [{"type": "refusal", "refusal": "Synthetic refusal"}]
            if outcome == "refusal"
            else []
            if outcome == "empty"
            else [{"type": "output_text", "text": '{"ok":true}'}]
        )
        response: dict[str, object] = {
            "id": "fixture",
            "status": status,
            "output": [{"type": "message", "content": content}],
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        }
        if outcome == "failed":
            response["error"] = {"code": "server_error", "message": "Synthetic failure"}
        if outcome == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        if outcome == "embedded-error":
            first["error"] = {"message": "Synthetic malformed stream"}
        events = [first, {"type": "response." + status, "response": response}]
    return 200, b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)


@pytest.mark.parametrize(
    "outcome,stream",
    [
        ("structured", False),
        ("structured", True),
        ("empty", False),
        ("refusal", False),
        ("failed", False),
        ("incomplete", False),
        ("http-error", False),
        ("sse-error", True),
        ("embedded-error", True),
        ("truncated-http", False),
    ],
)
def test_native_provider_outcome_is_recorded_and_replays_without_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    stream: bool,
) -> None:
    provider_dir = tmp_path / "provider"
    create(provider_dir, "localhost")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(provider_dir / "gateway.pem", provider_dir / "gateway.key")
    root = Path(__file__).resolve().parents[2]
    status, body = outcome_body(outcome)
    seen: list[bytes] = []
    partial_sent, release_completion, stream_returned = threading.Event(), threading.Event(), threading.Event()
    buffered = outcome == "structured" and stream
    if not buffered:
        release_completion.set()
    with socket.socket() as listener, ThreadPoolExecutor(max_workers=1) as pool:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(90)

        def provider() -> None:
            with context.wrap_socket(listener.accept()[0], server_side=True) as connection:
                connection.settimeout(90)
                probe = bytearray()
                while b"\r\n\r\n" not in probe:
                    probe.extend(connection.recv(65536))
                assert probe.startswith(b"HEAD /v1/responses HTTP/1.1\r\n")
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    request.extend(connection.recv(65536))
                head, request_body = bytes(request).split(b"\r\n\r\n", 1)
                length = int(
                    next(line for line in head.split(b"\r\n") if line.startswith(b"Content-Length:")).split(b":")[1]
                )
                while len(request_body) < length:
                    request_body += connection.recv(65536)
                seen.append(request_body)
                header = (
                    f"HTTP/1.1 {status} Fixture\r\n"
                    f"Content-Length: {len(body) + (7 if outcome == 'truncated-http' else 0)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                first, separator, rest = body.partition(b"\n\n")
                connection.sendall(header + first + separator)
                partial_sent.set()
                assert release_completion.wait(90)
                connection.sendall(rest)

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
            if result.returncode:
                if mode != "finish" or "Witness verification rejected the exchange" in result.stderr.decode():
                    raise ValueError("Native fixture rejected the captured evidence")
                raise RuntimeError("Native fixture verifier did not complete")
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
            kwargs.pop("host", None)
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
            async with fixture.running():

                def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
                    observed = []
                    for _ in range(1 if outcome in ("http-error", "sse-error", "embedded-error", "truncated-http") else 2):
                        try:
                            result = (
                                witness.create(model="fixture", input="Synthetic fixture", stream=True)
                                if stream
                                else witness.create(model="fixture", input="Synthetic fixture")
                            )
                            if stream:
                                stream_returned.set()
                                for event in cast(ModelStream, result):
                                    observed.append(event.model_dump(mode="json"))
                            else:
                                response = cast(Response, result)
                                observed.append(
                                    {
                                        "status": response.status,
                                        "text": response.output_text,
                                        "usage": response.usage.model_dump() if response.usage else None,
                                        "output": [item.model_dump() for item in response.output],
                                    }
                                )
                        except openai.APIError as error:
                            observed.append(
                                {
                                    "error": type(error).__name__,
                                    "code": error.code,
                                    "status": getattr(error, "status_code", None),
                                }
                            )
                    return json.dumps(observed, sort_keys=True)

                with fixture.client() as witness:
                    work = asyncio.create_task(
                        asyncio.to_thread(run_generation, generate, fixture.image, 42, witness, None)
                    )
                    try:
                        if buffered:
                            assert await asyncio.to_thread(partial_sent.wait, 90)
                            await asyncio.sleep(0.1)
                            assert not stream_returned.is_set(), "Streaming returned before the provider completed"
                            assert not work.done()
                        release_completion.set()
                        expected = await asyncio.wait_for(work, 180)
                    finally:
                        release_completion.set()
                    execution = next(witness._state.glob("executions/*/*/execution.json"))
                bundle = tmp_path / "bundle"
                await asyncio.to_thread(
                    export_bundle,
                    records=fixture.records,
                    evidence=fixture.state,
                    scope=fixture.scope,
                    execution=execution,
                    output=bundle,
                    proof_hash=fixture.proof,
                    pricing=PRICING,
                    verify=lambda provider, directory: verify("replay", directory, provider=provider),
                )
                replay = ReplayWitness(bundle)
                assert run_replay(generate, fixture.image, 42, replay, None) == expected
                if outcome in ("http-error", "sse-error", "embedded-error", "truncated-http"):
                    assert replay.budget.charges == {}
                if outcome == "structured" and not stream:
                    evidence_prefix = next(
                        k.removesuffix("manifest.json")
                        for k in fixture.state.objects
                        if k.startswith("rounds/") and k.endswith("manifest.json")
                    )
                    for name in (
                        "request.json",
                        "response.sse",
                        "manifest.json",
                        "client.bin",
                        "server.bin",
                        "authorization-server.bin",
                        "claim.json",
                    ):
                        object_key = evidence_prefix + name
                        original = fixture.state.objects[object_key]
                        for missing in (True, False):
                            if missing:
                                fixture.state.delete(object_key)
                            else:
                                fixture.state.put(object_key, b"corrupt synthetic evidence")
                            rejected = tmp_path / (name + ("-missing" if missing else "-corrupt"))
                            try:
                                with pytest.raises((ValueError, OSError)):
                                    await asyncio.to_thread(
                                        export_bundle,
                                        records=fixture.records,
                                        evidence=fixture.state,
                                        scope=fixture.scope,
                                        execution=execution,
                                        output=rejected,
                                        proof_hash=fixture.proof,
                                        pricing=PRICING,
                                        verify=lambda provider, directory: verify(
                                            "replay", directory, provider=provider
                                        ),
                                    )
                                assert not rejected.exists()
                            finally:
                                fixture.state.objects[object_key] = original
                assert len(seen) == fixture.provider.creates == 1
                for key, saved in fixture.state.objects.items():
                    assert ("fixture-" + "a" * 80).encode() not in saved.data, key

        asyncio.run(scenario())
        captured.result(timeout=5)

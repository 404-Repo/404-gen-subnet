"""Real TLS + early native proof over WebSockets; no OpenAI calls."""

from __future__ import annotations

import json
import socket
import ssl
import subprocess
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import cast

import httpx
import pytest
from bittensor_wallet import Keypair
from channel import WorkerChannel
from conftest import MemoryStore, certificates, connected_channels
from identity import Provider, miner_prefix
from storage import json_bytes
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close
from witness_client import WitnessClient, WitnessConfig, WitnessError
from witness_client.calls import RequestKey
from witness_client.gateway import GatewayClient
from witness_client.pods import CallView, PodGatewayClient, Snapshot
from witness_client.response import completed_response, http_body
from witness_client.transport import exchange
from witness_client.types import EarlyDisclosure, HandshakeDisclosure, Session
from worker import PROVIDERS, relay

WITNESS = Path(__file__).resolve().parents[2] / "verifier"


class MinerLeft(Exception):
    pass


@pytest.mark.parametrize(
    "abandon,usage_known,provider_status,terminal",
    [
        (False, True, 200, "completed"),
        (True, True, 200, "completed"),
        (False, False, 200, "completed"),
        (False, False, 400, "failed"),
        (False, False, 404, "failed"),
        (False, False, 401, "failed"),
        (False, False, 403, "failed"),
        (True, False, 429, "failed"),
        (False, False, 500, "failed"),
        (False, True, 200, "failed"),
        (False, True, 200, "incomplete"),
    ],
)
@pytest.mark.parametrize("provider_name", ["api", "codex"])
def test_real_tls_gate_records_large_exchange_even_when_miner_leaves(
    tmp_path: Path,
    abandon: bool,
    usage_known: bool,
    provider_status: int,
    terminal: str,
    provider_name: Provider,
    identity: tuple[Keypair, str],
    authorization_notification_lost: bool = False,
    before_release: Callable[[bytes], None] | None = None,
    session_id: str | None = None,
) -> None:
    hotkey, _ = identity
    certificates(tmp_path)
    directory = tmp_path / "witness"
    directory.mkdir()
    (directory / "ca.pem").write_bytes((tmp_path / "ca.pem").read_bytes())
    store = MemoryStore()
    identifier = str(uuid.uuid4())
    group = miner_prefix("1", hotkey.ss58_address)
    prefix_key = f"{group}/{identifier}"
    store.put(prefix_key + "/status.json", b'{"status":"running"}')
    credential = "fixture-" + "a" * 80
    path = PROVIDERS[provider_name][1]
    body = json_bytes({"model": "fixture", "input": "x" * (1024 * 1024)})
    text = "// fixture\n" + "y" * (1024 * 1024)
    response_body = (
        "data: "
        + json.dumps(
            {
                "type": f"response.{terminal}",
                "response": {
                    "status": terminal,
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    ],
                    "usage": (
                        {"input_tokens": 123, "output_tokens": 456, "total_tokens": 579}
                        if usage_known
                        else None
                    ),
                },
            }
        )
        + "\n\n"
    ).encode()
    if provider_status != 200:
        response_body = json_bytes({"error": {"message": "Fixture provider rejection"}})
    response = (
        f"HTTP/1.1 {provider_status} Fixture\r\nContent-Length: {len(response_body)}\r\nConnection: close\r\n\r\n".encode()
        + response_body
    )
    prefix = (
        f"POST {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: {len(body)}\r\nContent-Type: application/json\r\nAccept: text/event-stream\r\nAuthorization: Bearer "
    ).encode()
    suffix = (
        b"\r\n\r\n"
        if provider_name == "api"
        else b"\r\nChatGPT-Account-Id: abc123\r\n"
        + (f"session-id: {session_id}\r\n".encode() if session_id is not None else b"")
        + b"\r\n"
    ) + body
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(tmp_path / "leaf.pem", tmp_path / "key.pem")

    def verify(mode: str, directory: Path) -> dict[str, object]:
        if (
            mode == "handshake"
            and not abandon
            and usage_known
            and terminal == "completed"
        ):
            wrong_provider = "codex" if provider_name == "api" else "api"
            rejected = subprocess.run(
                [
                    "node",
                    str(Path(__file__).with_name("verify-fixture.mjs")),
                    mode,
                    str(directory),
                    str(WITNESS / "params"),
                    wrong_provider,
                ],
                capture_output=True,
                timeout=180,
                check=False,
            )
            assert rejected.returncode != 0
            assert b"Invalid HEAD probe" in rejected.stderr
        result = subprocess.run(
            [
                "node",
                str(Path(__file__).with_name("verify-fixture.mjs")),
                mode,
                str(directory),
                str(WITNESS / "params"),
                provider_name,
            ],
            capture_output=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode()
        return cast(dict[str, object], json.loads(result.stdout))

    def evidence(request: httpx.Request) -> httpx.Response:
        route = f"/v1/exchanges/{identifier}/"
        assert request.url.path.startswith(route)
        assert "authorization" not in request.headers
        assert request.headers["x-bittensor-hotkey"] == hotkey.ss58_address
        assert request.headers["x-bittensor-receiver"] == "404-witness"
        name = request.url.path.removeprefix(route)
        key = prefix_key + "/" + ("status.json" if name == "progress" else name)
        assert request.method == "GET"
        data = store.get(key)
        return (
            httpx.Response(200, content=data)
            if data is not None
            else httpx.Response(404, json={"error": "object_not_ready"})
        )

    config: WitnessConfig = {
        "witness_url": "https://witness.test",
        "proof_parameters": str(WITNESS / "params"),
        "state_directory": str(tmp_path / "client"),
    }
    with (
        WitnessClient(
            config,
            openai_api_key=credential if provider_name == "api" else None,
            subscription="codex" if provider_name == "codex" else None,
            hotkey=hotkey,
            delivery_timeout=180,
        ) as client,
        httpx.Client(
            base_url=client._http.base_url, transport=httpx.MockTransport(evidence)
        ) as http,
        socket.socket() as server,
        connected_channels(identifier) as (tunnel, channel),
    ):
        client._gateway.http = http
        (client._state / identifier).mkdir(mode=0o700)
        (client._state / identifier / "job.json").write_bytes(
            json_bytes({"deadline": int(time.time()) + 180})
        )
        server.bind(("127.0.0.1", 0))
        server.listen()

        def provider() -> bytes:
            with context.wrap_socket(
                server.accept()[0], server_side=True
            ) as connection:
                connection.settimeout(90)
                probe = bytearray()
                while b"\r\n\r\n" not in probe:
                    part = connection.recv(65536)
                    if not part:
                        raise RuntimeError("Incomplete fixture probe")
                    probe.extend(part)
                assert (
                    probe
                    == f"HEAD {path} HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n".encode()
                )
                assert credential.encode() not in probe
                assert store.get(prefix_key + "/authorization.json") is None
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: keep-alive\r\n\r\n"
                )
                request = bytearray()
                length = len(prefix) + len(credential) + len(suffix)
                while len(request) < length:
                    part = connection.recv(65536)
                    if not part:
                        raise RuntimeError("Incomplete fixture request")
                    request.extend(part)
                assert store.get(prefix_key + "/authorization.json") is not None
                assert store.get(prefix_key + "/request.json") == body
                connection.sendall(response)
                return bytes(request)

        with ThreadPoolExecutor(max_workers=2) as executor:
            actual_request = executor.submit(provider)

            run_relay = relay if before_release is None else partial(relay, before_release=before_release)

            def witness() -> dict[str, object]:
                try:
                    return run_relay(
                        store,
                        prefix_key,
                        time.monotonic() + 180,
                        directory,
                        verify,
                        channel,
                        round_number="1",
                        hotkey=hotkey.ss58_address,
                        host="127.0.0.1",
                        port=server.getsockname()[1],
                    )
                except Exception:
                    store.put(prefix_key + "/status.json", b'{"status":"failed"}')
                    raise

            observed = executor.submit(witness)
            capture_deadline = time.monotonic() + 180

            def handshake(disclosure: HandshakeDisclosure) -> None:
                tunnel.flush()
                client._handshake(tunnel, disclosure, deadline=capture_deadline)

            def authorize(disclosure: EarlyDisclosure) -> None:
                tunnel.flush()
                client._authorize(tunnel, disclosure, deadline=capture_deadline)
                if abandon:
                    tunnel.close()
                    raise MinerLeft

            try:
                raw = exchange(
                    tunnel,
                    "localhost",
                    [(False, prefix), (True, credential.encode()), (False, suffix)],
                    handshake=handshake,
                    authorize=authorize,
                    ca_file=str(tmp_path / "ca.pem"),
                    timeout=180,
                )
                assert not abandon
                if provider_status == 200:
                    assert http_body(raw) == response_body
                else:
                    with pytest.raises(WitnessError) as failure:
                        http_body(raw)
                    assert failure.value.status_code == provider_status
            except MinerLeft:
                assert abandon
            except WitnessError as error:
                assert authorization_notification_lost
                assert str(error) == "Witness closed before proof acceptance"
            finally:
                tunnel.close()
            metadata = observed.result(timeout=90)
            assert GatewayClient(http, hotkey, client._state, 180).response(identifier) == response_body
            if session_id is not None:
                invocation = RequestKey.from_body(provider_name, body, session_id=session_id)
                (client._state / identifier / "job.json").write_bytes(
                    json_bytes({"request": invocation.model_dump(mode="json")})
                )
                session: Session = {
                    "id": identifier, "protocol": 2, "round": "1",
                    "miner": hotkey.ss58_address,
                    "provider": f"https://{PROVIDERS[provider_name][0]}{path}",
                    "proof_key_sha256": client._prover.verifying_key_sha256,
                }
                call = CallView(
                    request=invocation, exchange_id=identifier,
                    deadline=int(time.time()) + 180, phase="issued", authorized=True,
                )
                assert isinstance(client._gateway, PodGatewayClient)
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(client._gateway, "snapshot", lambda _, **__: Snapshot(
                        call=call, hotkey=hotkey.ss58_address, session=dict(session),
                    ))
                    patch.setattr(client._gateway, "recorded", lambda *_, **__: (session, provider_status, response_body))
                    assert client.resume(identifier).output_text == text
            assert (
                actual_request.result(timeout=10)
                == prefix + credential.encode() + suffix
            )
    if provider_status == 200 and terminal == "completed":
        assert (
            completed_response(store.objects[prefix_key + "/response.sse"])[0] == text
        )
    assert store.objects[prefix_key + "/request.json"] == body
    manifest = json.loads(store.objects[prefix_key + "/manifest.json"])
    assert manifest["round"] == "1"
    assert manifest["hotkey"] == hotkey.ss58_address
    assert manifest["exchange_id"] == identifier
    assert manifest["provider_status"] == provider_status
    if usage_known:
        assert metadata["usage"] == {
            "input_tokens": 123,
            "output_tokens": 456,
            "total_tokens": 579,
        }
    elif provider_status in (400, 401, 403, 404, 429):
        assert metadata["usage"] is None
        assert (
            json.loads(store.objects[prefix_key + "/status.json"])["usage_state"]
            == "unknown"
        )
    else:
        assert metadata["usage"] is None
    assert all(credential.encode() not in value for value in store.objects.values())
    assert not any(
        "/transport/" in key
        or key.endswith(("/active.json", "/started.json", "/index.json"))
        for key in store.objects
    )
    assert store.writes.index(prefix_key + "/authorization.json") < store.writes.index(
        prefix_key + "/manifest.json"
    )


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionClosedOK(Close(1000, "disconnected"), Close(1000, "disconnected"), True),
        ConnectionClosedError(None, None),
        OSError("disconnected"),
    ],
    ids=["normal-close", "abnormal-close", "socket-error"],
)
def test_approved_capture_survives_lost_authorization_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[Keypair, str],
    failure: Exception,
) -> None:
    original_notify = WorkerChannel.notify

    def notify(channel: WorkerChannel, status: dict[str, object]) -> None:
        if status.get("authorized") is True:
            raise failure
        original_notify(channel, status)

    original_connect = socket.socket.connect

    def connect(connection: socket.socket, address: tuple[str, int]) -> None:
        assert address[0] in ("127.0.0.1", "::1"), "Only the local TLS fixture is permitted"
        original_connect(connection, address)

    reservations: list[bytes] = []
    monkeypatch.setattr(WorkerChannel, "notify", notify)
    monkeypatch.setattr(socket.socket, "connect", connect)
    test_real_tls_gate_records_large_exchange_even_when_miner_leaves(
        tmp_path, False, True, 200, "completed", "api", identity,
        authorization_notification_lost=True, before_release=reservations.append,
    )
    assert len(reservations) == 1

"""Real TLS over mocked async frames, plus delivery cancellation and cleanup."""

from __future__ import annotations

import asyncio
import datetime
import json
import ssl
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from bittensor_wallet import Keypair
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from witness_client import WitnessError
from witness_client.async_pods import AsyncSignedTransport
from witness_client.async_transport import async_exchange
from witness_client.async_tunnel import AsyncWebSocketTunnel
from witness_client.delivery import async_request, async_retry
from witness_client.proof import Prover
from witness_client.response import http_body
from witness_client.types import EarlyDisclosure, HandshakeDisclosure
from test_tunnel import Socket

PARAMS = Path(__file__).resolve().parents[2] / "witness-server/verifier/params"


@pytest.fixture
def tls_context(tmp_path: Path) -> tuple[ssl.SSLContext, Path]:
    root_key, leaf_key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(2))
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Async fixture CA")])
    now = datetime.datetime.now(datetime.UTC)
    for name, key, is_ca in [("ca", root_key, True), ("leaf", leaf_key, False)]:
        subject = (
            root_name
            if is_ca
            else x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(root_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(True, False, False, False, False, is_ca, is_ca, False, False),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
                critical=False,
            )
        )
        if not is_ca:
            cert = cert.add_extension(
                x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
            ).add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
        (tmp_path / f"{name}.pem").write_bytes(
            cert.sign(root_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        )
    (tmp_path / "key.pem").write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(tmp_path / "leaf.pem", tmp_path / "key.pem")
    return context, tmp_path / "ca.pem"


def test_async_tls_captures_large_request_with_real_native_credential_proof(
    tls_context: tuple[ssl.SSLContext, Path],
) -> None:
    context, ca_file = tls_context
    prefix = b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
    credential = b"fixture-" + b"x" * 80
    suffix = b"\r\nContent-Length: 300000\r\n\r\n" + b"q" * 300000
    expected = prefix + credential + suffix
    response_body = b"r" * 350000
    response = b"HTTP/1.1 200 OK\r\nContent-Length: 350000\r\n\r\n" + response_body

    class Provider:
        def __init__(self) -> None:
            self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
            self.tls = context.wrap_bio(self.incoming, self.outgoing, server_side=True)
            self.handshook = self.probed = self.authorized = self.completed = False
            self.request = bytearray()
            self.held = bytearray()
            self.frames: deque[str | bytes] = deque()
            self.phases: list[str] = []

        def pump(self) -> None:
            try:
                if not self.handshook:
                    self.tls.do_handshake()
                    self.handshook = True
                while True:
                    self.request.extend(self.tls.read(65536))
                    if not self.probed and self.request.endswith(b"\r\n\r\n"):
                        assert (
                            self.request
                            == b"HEAD /v1/responses HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n"
                        )
                        self.request.clear()
                        self.probed = True
                        self.tls.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                    elif self.authorized and len(self.request) == len(expected):
                        assert self.request == expected
                        self.tls.write(response)
                        self.completed = True
                        self.tls.unwrap()
            except ssl.SSLWantReadError:
                pass
            while self.outgoing.pending:
                self.frames.append(self.outgoing.read(65536))
            if self.completed:
                self.frames.append('{"type":"end"}')

        async def send(self, message: str | bytes) -> None:
            await asyncio.sleep(0)
            if isinstance(message, bytes):
                if self.probed and not self.authorized:
                    self.held.extend(message)
                    return
                self.incoming.write(message)
                self.pump()
            else:
                value = json.loads(message)
                self.phases.append(value["type"])
                if value["type"] == "handshake":
                    self.frames.append('{"type":"status","status":"running","handshook":true}')
                else:
                    assert self.probed and self.held
                    assert "credential" in value["claim"]
                    self.authorized = True
                    self.frames.append('{"type":"status","status":"running","authorized":true}')
                    self.incoming.write(self.held)
                    self.held.clear()
                    self.pump()

        async def recv(self) -> str | bytes:
            await asyncio.sleep(0)
            assert self.frames, "TLS driver must send pending bytes before waiting for input"
            return self.frames.popleft()

        async def close(self) -> None:
            pass

    async def run() -> None:
        provider = Provider()
        tunnel = AsyncWebSocketTunnel(provider, "fixture")
        prover = await asyncio.to_thread(Prover, PARAMS)

        async def handshake(disclosure: HandshakeDisclosure) -> None:
            assert not provider.authorized and credential not in provider.request
            await tunnel.verify("handshake", b"{}", 5)

        async def authorize(disclosure: EarlyDisclosure) -> None:
            assert len(disclosure["private"]) == 2
            assert (
                len([record for record in disclosure["disclosures"]["client"] if not record]) == 2
            )
            proof = await asyncio.to_thread(prover.prove, disclosure["private"])
            await tunnel.verify("authorize", json.dumps({"credential": proof}).encode(), 5)

        result = await async_exchange(
            tunnel,
            "localhost",
            [(False, prefix), (True, credential), (False, suffix)],
            handshake=handshake,
            authorize=authorize,
            ca_file=ca_file,
            timeout=30,
        )
        assert result == response and http_body(result) == response_body
        assert provider.phases == ["handshake", "authorize"]

    asyncio.run(run())


def test_async_tunnel_proof_order_and_pending_tls_use_existing_frames() -> None:
    socket = Socket(
        [
            b"provider TLS",
            '{"type":"status","status":"running","authorized":true}',
            '{"type":"end"}',
        ]
    )

    class AsyncSocket:
        async def send(self, message: str | bytes) -> None:
            socket.send(message)

        async def recv(self) -> str | bytes:
            return socket.recv()

        async def close(self) -> None:
            socket.close()

    async def run() -> None:
        tunnel = AsyncWebSocketTunnel(AsyncSocket(), "fixture")
        await tunnel.send(b"request TLS")
        assert (await tunnel.verify("authorize", b"{}", 1))["authorized"]
        assert socket.sent == [b"request TLS", '{"type":"authorize","claim":{}}']
        assert await tunnel.recv(1) == b"provider TLS"
        assert await tunnel.recv(1) == b""

    asyncio.run(run())


def test_cancelled_signed_websocket_receive_closes_connection_without_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        receiving = asyncio.Event()
        closed: list[bool] = []
        connections: list[str] = []
        key = Keypair.create_from_uri("//Alice")
        tls = ssl.create_default_context()

        class Socket:
            async def send(self, message: str | bytes) -> None:
                pass

            async def recv(self) -> bytes:
                receiving.set()
                await asyncio.Event().wait()
                return b""

            async def close(self) -> None:
                closed.append(True)

        @asynccontextmanager
        async def connect(url: str, **kwargs: object) -> AsyncIterator[Socket]:
            connections.append(url)
            assert kwargs["ssl"] is tls and kwargs["server_hostname"] == "witness.internal"
            assert kwargs["proxy"] is kwargs["compression"] is None
            headers = kwargs["additional_headers"]
            assert isinstance(headers, dict) and headers["Authorization"] == "Bearer pod-access"
            assert headers["x-bittensor-hotkey"] == key.ss58_address
            socket = Socket()
            try:
                yield socket
            finally:
                await socket.close()

        monkeypatch.setattr("witness_client.async_pods.connect", connect)
        async with httpx.AsyncClient(base_url="https://192.0.2.1:443") as http:
            signed = AsyncSignedTransport(
                http,
                key,
                1,
                tls=tls,
                server_name="witness.internal",
                headers={"Authorization": "Bearer pod-access"},
            )

            async def receive() -> None:
                async with signed.tunnel("fixture") as tunnel:
                    await tunnel.recv(1)

            task = asyncio.create_task(receive())
            await asyncio.wait_for(receiving.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert closed == [True] and connections == ["wss://192.0.2.1/v1/exchanges/fixture/connect"]

    asyncio.run(run())


def test_async_delivery_cancellation_closes_stream_and_stops_retry_waits() -> None:
    async def run() -> None:
        reading, retrying = asyncio.Event(), asyncio.Event()
        closed: list[bool] = []
        attempts = 0

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                reading.set()
                await asyncio.Event().wait()
                yield b"unavailable"

            async def aclose(self) -> None:
                closed.append(True)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))
        ) as http:
            task = asyncio.create_task(
                async_request(http, "GET", "https://fixture.test", headers={})
            )
            await asyncio.wait_for(reading.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed == [True]

        async def transient() -> bytes:
            nonlocal attempts
            attempts += 1
            retrying.set()
            raise WitnessError("temporary", status_code=503)

        task = asyncio.create_task(
            async_retry(transient, deadline=asyncio.get_running_loop().time() + 30)
        )
        await asyncio.wait_for(retrying.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert attempts == 1

    asyncio.run(run())

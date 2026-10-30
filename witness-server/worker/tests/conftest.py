from __future__ import annotations

import datetime
import threading

# Real WebSockets keep the TLS integration tests on the production framing.
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from queue import Queue

import pytest
from bittensor_wallet import Keypair
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from websockets.sync.client import connect
from websockets.sync.server import ServerConnection, serve
from witness_client.tunnel import WebSocketTunnel

from channel import WorkerChannel
from storage import MAX_BYTES


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> tuple[Keypair, str]:
    miner = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    other = Keypair.create_from_mnemonic(Keypair.generate_mnemonic()).ss58_address
    monkeypatch.setenv("WITNESS_ROUND", "1")
    return miner, other


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.writes: list[str] = []
        self.lock = threading.Lock()

    def get(self, key: str, limit: int = MAX_BYTES) -> bytes | None:
        with self.lock:
            result = self.objects.get(key)
        if result is not None and len(result) > limit:
            raise ValueError("Object exceeds limit")
        return result

    def put(self, key: str, data: bytes) -> None:
        with self.lock:
            self.objects[key] = data
            self.writes.append(key)

    def create(self, key: str, data: bytes) -> bool:
        with self.lock:
            if key in self.objects:
                return False
            self.objects[key] = data
            return True

    def delete(self, key: str) -> None:
        with self.lock:
            self.objects.pop(key, None)


def certificates(directory: Path) -> None:
    root_key = ec.generate_private_key(ec.SECP256R1())
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    root_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Witness fixture CA")]
    )

    def builder(
        public: ec.EllipticCurvePublicKey, subject: x509.Name, issuer: x509.Name
    ) -> x509.CertificateBuilder:
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(public)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
        )

    root = (
        builder(root_key.public_key(), root_name, root_name)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    leaf = (
        builder(
            key.public_key(),
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]),
            root_name,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    for name, cert in [("ca.pem", root), ("leaf.pem", leaf)]:
        (directory / name).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


@contextmanager
def connected_channels(
    identifier: str,
) -> Iterator[tuple[WebSocketTunnel, WorkerChannel]]:
    ready: Queue[ServerConnection] = Queue()
    stop = threading.Event()

    def accept(connection: ServerConnection) -> None:
        ready.put(connection)
        stop.wait(190)

    with serve(
        accept, "127.0.0.1", 0, compression=None, max_size=2 * 1024 * 1024
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with connect(
                f"ws://127.0.0.1:{server.socket.getsockname()[1]}",
                proxy=None,
                compression=None,
                max_size=256 * 1024,
            ) as connection:
                yield WebSocketTunnel(connection, identifier), WorkerChannel(
                    ready.get(timeout=5)
                )
        finally:
            stop.set()
            server.shutdown()
            thread.join(timeout=5)

"""These tests must never reach a real provider or R2 endpoint."""

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    original = socket.socket.connect

    def blocked(sock: socket.socket, address: tuple[str, int] | tuple[str, int, int, int]) -> None:
        if address[0] in ("127.0.0.1", "::1"):
            return original(sock, address)
        raise AssertionError("Network access is forbidden in allocation tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)

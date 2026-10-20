"""These tests must never reach a real provider or R2 endpoint."""

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network access is forbidden in allocation tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Miner tests must not contact providers or services")

    monkeypatch.setattr(socket.socket, "connect", blocked)

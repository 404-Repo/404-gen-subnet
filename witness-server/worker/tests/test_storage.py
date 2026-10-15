"""A lost R2 acknowledgement is recoverable only from the exact saved bytes."""

from __future__ import annotations

import io
import json
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from bittensor_wallet import Keypair
from botocore.exceptions import ClientError, ConnectionClosedError, ReadTimeoutError

import worker
from identity import signed_exchange
from storage import R2Store


@pytest.fixture
def r2(monkeypatch: pytest.MonkeyPatch) -> tuple[R2Store, MagicMock]:
    client = MagicMock()
    for name in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "fixture")
    monkeypatch.setattr(boto3, "client", MagicMock(return_value=client))
    return R2Store(), client


@pytest.mark.parametrize(
    "error",
    [
        ReadTimeoutError(endpoint_url="https://storage.test"),
        ConnectionClosedError(endpoint_url="https://storage.test"),
    ],
)
def test_put_recovers_only_an_exact_saved_object(
    r2: tuple[R2Store, MagicMock], error: Exception
) -> None:
    store, client = r2
    client.put_object.side_effect = error
    body = io.BytesIO(b"saved evidence")
    client.get_object.return_value = {"Body": body}

    store.put("evidence", b"saved evidence")

    client.put_object.assert_called_once()
    client.get_object.assert_called_once_with(Bucket=store.bucket, Key="evidence")
    assert body.closed


@pytest.mark.parametrize("saved", [None, b"different", b"expected plus extra"])
def test_unconfirmed_put_still_fails(
    r2: tuple[R2Store, MagicMock], saved: bytes | None
) -> None:
    store, client = r2
    client.put_object.side_effect = ReadTimeoutError(
        endpoint_url="https://storage.test"
    )
    if saved is None:
        client.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )
    else:
        client.get_object.return_value = {"Body": io.BytesIO(saved)}

    with pytest.raises((ReadTimeoutError, ValueError)):
        store.put("evidence", b"expected")


def test_failed_readback_does_not_confirm_a_put(r2: tuple[R2Store, MagicMock]) -> None:
    store, client = r2
    client.put_object.side_effect = ReadTimeoutError(
        endpoint_url="https://storage.test"
    )
    client.get_object.side_effect = ReadTimeoutError(
        endpoint_url="https://storage.test"
    )
    with pytest.raises(ReadTimeoutError):
        store.put("evidence", b"expected")


@pytest.mark.parametrize("denied", [False, True])
def test_normal_success_and_access_denial_do_not_read_back(
    r2: tuple[R2Store, MagicMock], denied: bool
) -> None:
    store, client = r2
    if denied:
        client.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "PutObject"
        )
        with pytest.raises(ClientError):
            store.put("evidence", b"expected")
    else:
        store.put("evidence", b"expected")
    client.get_object.assert_not_called()


@pytest.mark.parametrize("saved", [True, False])
def test_worker_announces_ready_only_after_confirmed_session(
    r2: tuple[R2Store, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[Keypair, str],
    saved: bool,
) -> None:
    store, client = r2
    objects: dict[str, bytes] = {}

    def put_object(*, Key: str, Body: bytes, **kwargs: object) -> None:
        if saved or not Key.endswith("/session.json"):
            objects[Key] = Body
        if Key.endswith("/session.json"):
            raise ReadTimeoutError(endpoint_url="https://storage.test")

    def get_object(*, Key: str, **kwargs: object) -> dict[str, object]:
        if Key not in objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(objects[Key])}

    client.put_object.side_effect = put_object
    client.get_object.side_effect = get_object
    monkeypatch.setattr(worker, "R2Store", lambda: store)
    monkeypatch.setattr(worker, "PARAMETERS", tmp_path)
    (tmp_path / "verifying.bin").write_bytes(b"fixture-key")
    connection = MagicMock()
    monkeypatch.setattr(worker, "connect", MagicMock(return_value=connection))
    relay = MagicMock(return_value={})
    monkeypatch.setattr(worker, "relay", relay)
    hotkey, _ = identity
    expires = int(time.time()) + 60
    job: dict[str, object] = {
        "input": {
            **signed_exchange(hotkey, str(uuid.uuid4()), expires, provider="codex"),
            "round": "1",
            "expires": expires,
            "gateway": "https://gateway.test",
            "token": "a" * 64,
        }
    }
    if saved:
        worker.handler(job)
        message = json.loads(connection.send.call_args.args[0])
        assert message["type"] == "ready"
        assert (
            message["session"]["provider"]
            == "https://chatgpt.com/backend-api/codex/responses"
        )
        relay.assert_called_once()
    else:
        with pytest.raises(RuntimeError, match="ReadTimeoutError"):
            worker.handler(job)
        connection.send.assert_not_called()
        relay.assert_not_called()

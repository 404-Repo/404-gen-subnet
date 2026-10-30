"""A lost R2 acknowledgement is recoverable only from the exact saved bytes."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError, ConnectionClosedError, ReadTimeoutError

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

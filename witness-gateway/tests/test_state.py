from __future__ import annotations

from io import BytesIO
from typing import cast

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from pod_gateway.state import MAX_JOURNAL_BYTES, R2StateStore, StateConflict, WriteRejected, WriteUncertain


def s3_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "PutObject",
    )


class FakeS3:
    def __init__(self) -> None:
        self.data: bytes | None = None
        self.etag = '"1"'
        self.puts: list[dict[str, object]] = []
        self.gets = 0
        self.mode = "ok"
        self.throttles = 0
        self.body: BytesIO | None = None
        self.read_error: ClientError | None = None

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.gets += 1
        assert kwargs == {"Bucket": "private", "Key": "journal"}
        if self.read_error is not None:
            raise self.read_error
        if self.data is None:
            raise s3_error("NoSuchKey", 404)
        self.body = BytesIO(self.data)
        return {"Body": self.body, "ETag": self.etag}

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self.puts.append(kwargs)
        if self.throttles:
            self.throttles -= 1
            raise s3_error("TooManyRequests", 429)
        if self.mode.startswith("status-"):
            status = int(self.mode.removeprefix("status-"))
            raise s3_error(str(status), status)
        if self.mode == "timeout-before":
            raise ReadTimeoutError(endpoint_url="https://r2.invalid")
        if (
            (kwargs.get("IfNoneMatch") == "*" and self.data is not None)
            or ("IfMatch" in kwargs and kwargs["IfMatch"] != self.etag)
        ):
            raise s3_error("PreconditionFailed", 412)
        self.data = cast(bytes, kwargs["Body"])
        self.etag = f'"{len(self.puts) + 1}"'
        if self.mode == "timeout-after":
            raise ReadTimeoutError(endpoint_url="https://r2.invalid")
        return {"ETag": self.etag}


def test_create_and_update_use_atomic_preconditions() -> None:
    s3 = FakeS3()
    store = R2StateStore(s3, "private")
    first = store.save("journal", b"first-revision", None)
    assert s3.puts[0] == {
        "Bucket": "private",
        "Key": "journal",
        "Body": b"first-revision",
        "ContentType": "application/json",
        "CacheControl": "no-store",
        "IfNoneMatch": "*",
    }
    second = store.save("journal", b"next-revision", first.etag)
    assert s3.puts[1]["IfMatch"] == first.etag
    assert "IfNoneMatch" not in s3.puts[1]
    assert store.load("journal") == second
    assert s3.body is not None and s3.body.closed
    with pytest.raises(StateConflict):
        store.save("journal", b"competing-revision", first.etag)
    assert s3.data == b"next-revision"


@pytest.mark.parametrize("status", [409, 412])
def test_precondition_failure_does_not_read_back_or_retry(status: int) -> None:
    s3 = FakeS3()
    s3.mode = f"status-{status}"
    with pytest.raises(StateConflict):
        R2StateStore(s3, "private").save("journal", b"revision", None)
    assert len(s3.puts) == 1 and s3.gets == 0


def test_lost_acknowledgement_is_confirmed_only_by_exact_readback() -> None:
    s3 = FakeS3()
    s3.mode = "timeout-after"
    snapshot = R2StateStore(s3, "private").save("journal", b"unique-revision", None)
    assert snapshot.data == b"unique-revision" and snapshot.etag == s3.etag
    assert len(s3.puts) == 1 and s3.gets == 1


@pytest.mark.parametrize("previous", [None, b"a-different-revision"])
def test_missing_or_different_readback_cannot_authorize_side_effects(
    previous: bytes | None,
) -> None:
    s3 = FakeS3()
    s3.data, s3.mode = previous, "timeout-before"
    with pytest.raises(WriteUncertain):
        R2StateStore(s3, "private").save("journal", b"unique-revision", s3.etag)
    assert len(s3.puts) == 1 and s3.gets == 1


def test_server_error_requires_readback_and_never_retries_write() -> None:
    s3 = FakeS3()
    s3.mode = "status-503"
    with pytest.raises(WriteUncertain):
        R2StateStore(s3, "private").save("journal", b"revision", None)
    assert len(s3.puts) == 1 and s3.gets == 1


def test_readback_failure_is_uncertain() -> None:
    s3 = FakeS3()
    s3.mode, s3.read_error = "timeout-after", s3_error("AccessDenied", 403)
    with pytest.raises(WriteUncertain):
        R2StateStore(s3, "private").save("journal", b"revision", None)


def test_access_denied_is_never_treated_as_a_missing_journal() -> None:
    s3 = FakeS3()
    store = R2StateStore(s3, "private")
    assert store.load("journal") is None
    s3.read_error = s3_error("AccessDenied", 403)
    with pytest.raises(ClientError):
        store.load("journal")
    s3.mode = "status-403"
    with pytest.raises(WriteRejected):
        store.save("journal", b"revision", None)


def test_journal_size_limit_closes_body_and_forbids_oversized_writes() -> None:
    s3 = FakeS3()
    s3.data = b"x" * (MAX_JOURNAL_BYTES + 1)
    store = R2StateStore(s3, "private")
    with pytest.raises(ValueError):
        store.load("journal")
    assert s3.body is not None and s3.body.closed
    with pytest.raises(ValueError):
        store.save("journal", s3.data, None)
    assert s3.puts == []


def test_result_index_can_use_a_larger_limit_without_changing_journal_limits() -> None:
    s3 = FakeS3()
    data = b"x" * (MAX_JOURNAL_BYTES + 1)
    store = R2StateStore(s3, "private", max_bytes=2 * MAX_JOURNAL_BYTES)
    store.save("journal", data, None)
    saved = store.load("journal")
    assert saved is not None and saved.data == data
    with pytest.raises(ValueError):
        R2StateStore(s3, "private").load("journal")


def test_rate_limited_writes_retry_then_save(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = FakeS3()
    s3.throttles = 2
    delays: list[float] = []
    monkeypatch.setattr("time.sleep", delays.append)
    store = R2StateStore(s3, "private")
    saved = store.save("journal", b"result-index", None)
    assert store.load("journal") == saved
    assert saved.data == b"result-index"
    assert delays == [1, 2]


def test_rate_limited_writes_stop_after_five_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = FakeS3()
    s3.throttles = 10
    delays: list[float] = []
    monkeypatch.setattr("time.sleep", delays.append)
    with pytest.raises(WriteRejected):
        R2StateStore(s3, "private").save("journal", b"result-index", None)
    assert len(s3.puts) == 5 and s3.data is None
    assert delays == [1, 2, 4, 8]


def test_rate_limit_retry_cannot_overwrite_a_concurrent_write(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = FakeS3()
    store = R2StateStore(s3, "private")
    first = store.save("journal", b"first", None)
    s3.throttles = 1

    def competing_write(delay: float) -> None:
        s3.data = b"another-writer"
        s3.etag = '"another-etag"'

    monkeypatch.setattr("time.sleep", competing_write)
    with pytest.raises(StateConflict):
        store.save("journal", b"replacement", first.etag)
    saved = store.load("journal")
    assert saved is not None and saved.data == b"another-writer"


def test_client_disables_implicit_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    s3 = FakeS3()

    def client(service: str, **kwargs: object) -> FakeS3:
        assert service == "s3"
        captured.update(kwargs)
        return s3

    monkeypatch.setattr("pod_gateway.state.boto3.client", client)
    store = R2StateStore.connect(
        endpoint="https://r2.invalid", access_key="offline", secret_key="offline", bucket="private"
    )
    assert store.client is s3
    config = captured["config"]
    assert getattr(config, "retries") == {"total_max_attempts": 1}

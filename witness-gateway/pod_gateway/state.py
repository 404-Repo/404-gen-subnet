"""Bounded R2 inference/replay records with atomic ETag preconditions."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, cast

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

MAX_JOURNAL_BYTES = 1024 * 1024


class StateConflict(RuntimeError):
    """Another request changed the journal; reload before doing any work."""


class WriteRejected(RuntimeError):
    """The storage service rejected the write without applying it."""


class WriteUncertain(RuntimeError):
    """A write could have committed. No provider action is permitted on this path."""


@dataclass(frozen=True)
class Snapshot:
    etag: str
    data: bytes


class StateStore(Protocol):
    def load(self, key: str) -> Snapshot | None: ...
    def save(self, key: str, data: bytes, expected: str | None) -> Snapshot: ...


class PrefixedState:
    """Apply a round's storage namespace to calls and accounting journals."""

    def __init__(self, store: StateStore, prefix: str) -> None:
        self.store, self.prefix = store, prefix

    def load(self, key: str) -> Snapshot | None:
        return self.store.load(self.prefix + key)

    def save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
        return self.store.save(self.prefix + key, data, expected)


class Body(Protocol):
    def read(self, amount: int) -> bytes: ...
    def close(self) -> None: ...


class S3(Protocol):
    def get_object(self, **kwargs: object) -> dict[str, object]: ...
    def put_object(self, **kwargs: object) -> dict[str, object]: ...


class R2StateStore:
    def __init__(self, client: S3, bucket: str, *, max_bytes: int = MAX_JOURNAL_BYTES) -> None:
        self.client = client
        self.bucket = bucket
        self.max_bytes = max_bytes

    @classmethod
    def connect(
        cls, *, endpoint: str, access_key: str, secret_key: str, bucket: str
    ) -> R2StateStore:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name="auto",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                connect_timeout=5,
                read_timeout=10,
                retries={"total_max_attempts": 1},
            ),
        )
        return cls(cast(S3, client), bucket)

    def load(self, key: str) -> Snapshot | None:
        return self._load(key, self.max_bytes)

    def read(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None:
        snapshot = self._load(key, limit)
        return None if snapshot is None else snapshot.data

    def _load(self, key: str, limit: int) -> Snapshot | None:
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        body = cast(Body, result["Body"])
        try:
            data = body.read(limit + 1)
        finally:
            body.close()
        if len(data) > limit:
            raise ValueError("Allocation journal exceeds its size limit")
        return Snapshot(self._etag(result), data)

    def save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
        if len(data) > self.max_bytes:
            raise ValueError("Allocation journal exceeds its size limit")
        condition = {"IfNoneMatch": "*"} if expected is None else {"IfMatch": expected}
        for attempt in range(5):
            try:
                result = self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    ContentType="application/json",
                    CacheControl="no-store",
                    **condition,
                )
                return Snapshot(self._etag(result), data)
            except ClientError as error:
                status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if status == 429 and attempt < 4:
                    time.sleep(2**attempt)
                    continue
                if status in (409, 412):
                    raise StateConflict("Allocation journal changed") from None
                if status is not None and status < 500:
                    if (
                        status >= 400
                        and status != 408
                        and error.response["Error"]["Code"] not in ("RequestTimeout", "RequestTimeoutException")
                    ):
                        raise WriteRejected("Allocation journal write was rejected") from error
                    raise
            except BotoCoreError:
                pass
            break
        # The unique journal revision makes an exact readback identify OUR write.
        # A missing/different object never proves the timed-out write cannot arrive.
        try:
            saved = self.load(key)
        except (BotoCoreError, ClientError, ValueError):
            raise WriteUncertain("Allocation journal write is unconfirmed") from None
        if saved is not None and saved.data == data:
            return saved
        raise WriteUncertain("Allocation journal write is unconfirmed")

    @staticmethod
    def _etag(result: dict[str, object]) -> str:
        etag = result.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise WriteUncertain("R2 did not return a journal ETag")
        return etag

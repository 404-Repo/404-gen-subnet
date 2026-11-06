"""Durable witness evidence in R2."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Protocol, TypedDict, cast
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectionClosedError, ReadTimeoutError
from timing import timed

MAX_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 256 * 1024


class Store(Protocol):
    def get(self, key: str, limit: int = MAX_BYTES) -> bytes | None: ...
    def put(self, key: str, data: bytes) -> None: ...
    def delete(self, key: str) -> None: ...


class Body(Protocol):
    def read(self, amount: int) -> bytes: ...
    def close(self) -> None: ...


class ListedObject(TypedDict):
    Key: str


class ObjectPage(TypedDict, total=False):
    Contents: list[ListedObject]
    IsTruncated: bool
    NextContinuationToken: str


class S3(Protocol):
    def get_object(self, **kwargs: object) -> dict[str, object]: ...
    def head_object(self, **kwargs: object) -> dict[str, object]: ...
    def put_object(self, **kwargs: object) -> dict[str, object]: ...
    def delete_object(self, **kwargs: object) -> object: ...
    def list_objects_v2(self, **kwargs: object) -> ObjectPage: ...
    def generate_presigned_url(self, operation: str, **kwargs: object) -> str: ...


class R2Store:
    @timed("r2_initialize")
    def __init__(
        self, bucket: str | None = None, *, fallback_bucket: str | None = None, public_url: str | None = None,
    ) -> None:
        self.bucket = bucket or os.environ.get("R2_BUCKET", "404-gen")
        self.fallback_bucket = fallback_bucket if fallback_bucket != self.bucket else None
        self.public_url = public_url
        self.client = cast(
            S3,
            boto3.client(
                "s3",
                endpoint_url=os.environ["R2_ENDPOINT"],
                region_name="auto",
                aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                config=Config(
                    connect_timeout=5,
                    read_timeout=10,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            ),
        )

    def get(self, key: str, limit: int = MAX_BYTES) -> bytes | None:
        data = self._get(self.bucket, key, limit)
        if data is None and self.fallback_bucket is not None:
            return self._get(self.fallback_bucket, key, limit)
        return data

    def read(self, key: str, limit: int = MAX_BYTES) -> bytes | None:
        return self.get(key, limit)

    def keys(self, prefix: str) -> Iterator[str]:
        continuation: dict[str, str] = {}
        while True:
            page = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix, **continuation)
            for item in page.get("Contents", []):
                yield item["Key"]
            if not page.get("IsTruncated", False):
                return
            continuation = {"ContinuationToken": page["NextContinuationToken"]}

    def _get(self, bucket: str, key: str, limit: int) -> bytes | None:
        try:
            result = self.client.get_object(Bucket=bucket, Key=key)
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
            raise ValueError("R2 object exceeds the exchange limit")
        return data

    @timed("r2_put")
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                CacheControl="no-store",
            )
        except (ReadTimeoutError, ConnectionClosedError):
            # R2 may have saved the object before its acknowledgement was lost.
            if self._get(self.bucket, key, len(data)) != data:
                raise

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def url(self, key: str) -> str:
        bucket = self.bucket
        if self.fallback_bucket is not None:
            try:
                self.client.head_object(Bucket=bucket, Key=key)
            except ClientError as error:
                if error.response["Error"]["Code"] not in ("NoSuchKey", "NotFound", "404"):
                    raise
                bucket = self.fallback_bucket
        if bucket == self.bucket and self.public_url is not None:
            return self.public_url.rstrip("/") + "/" + quote(key, safe="/")
        return str(self.client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=600,
        ))


def json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()

"""Durable witness evidence in R2."""

from __future__ import annotations

import json
import os
from typing import Protocol, cast

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
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


class S3(Protocol):
    def get_object(self, **kwargs: object) -> dict[str, object]: ...
    def put_object(self, **kwargs: object) -> object: ...
    def delete_object(self, **kwargs: object) -> object: ...


class R2Store:
    @timed("r2_initialize")
    def __init__(self) -> None:
        self.bucket = os.environ.get("R2_BUCKET", "404-gen")
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
            raise ValueError("R2 object exceeds the exchange limit")
        return data

    @timed("r2_put")
    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType="application/octet-stream",
            CacheControl="no-store",
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()

from types import TracebackType
from typing import Self

import aioboto3
from types_aiobotocore_s3 import S3Client


class R2Client:
    def __init__(
        self,
        access_key_id: str,
        secret_access_key: str,
        r2_endpoint: str,
        bucket: str = "404-gen",
    ):
        self.endpoint = r2_endpoint
        self.bucket = bucket
        self.session = aioboto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )
        self._client: S3Client | None = None

    @property
    def client(self) -> S3Client:
        """Get S3 client, raising if not initialized."""
        if self._client is None:
            raise RuntimeError("R2Client not initialized. Use 'async with R2Client(...)'")
        return self._client

    async def __aenter__(self) -> Self:
        self._client = await self.session.client(
            "s3",
            endpoint_url=self.endpoint,
        ).__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._client:
            await self._client.__aexit__(exc_type, exc_val, exc_tb)

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        await self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return key

    async def download(self, key: str) -> bytes:
        response = await self.client.get_object(Bucket=self.bucket, Key=key)
        body: bytes = await response["Body"].read()
        return body

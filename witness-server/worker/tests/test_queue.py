"""Worker channels preserve fragmented TLS bytes in both directions."""

from __future__ import annotations

import time
import uuid

from conftest import connected_channels

from storage import CHUNK_BYTES


def test_fragmented_megabyte_stream() -> None:
    identifier = str(uuid.uuid4())
    data = b"ciphertext" * 200_000
    with connected_channels(identifier) as (tunnel, channel):
        tunnel.send(data)
        tunnel.flush()
        result = bytearray()
        while len(result) < len(data):
            chunk = channel.recv(time.monotonic() + 1)
            assert len(chunk) <= CHUNK_BYTES
            result.extend(chunk)
        assert result == data
        channel.send(data)
        channel.finish()
        result.clear()
        while chunk := tunnel.recv(timeout=1):
            result.extend(chunk)
        assert result == data

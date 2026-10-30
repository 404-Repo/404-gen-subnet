"""Direct TLS connections have a deadline before their first HTTP request."""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path
from typing import cast

import pytest
from aiohttp import web
from pod_gateway.certs import pod_context


@pytest.mark.parametrize(
    "initial_request,complete",
    [
        pytest.param(b"", False, id="idle"),
        pytest.param(
            b"GET / HTTP/1.1\r\nHost: witness.internal\r\nX-Slow: ",
            False,
            id="incomplete-headers",
        ),
        pytest.param(b"GET / HTTP/1.1\r\nHost: witness.internal\r\n\r\n", True, id="ordinary-request"),
    ],
)
def test_direct_tls_first_request_deadline(
    tmp_path: Path, initial_request: bytes, complete: bool
) -> None:
    async def scenario() -> None:
        handled: list[str] = []

        async def handle(request: web.Request) -> web.Response:
            handled.append(request.path)
            await asyncio.sleep(0.2)
            return web.Response(text="ok")

        tls, ca = pod_context(tmp_path / "tls")
        client_tls = ssl.create_default_context(cadata=ca)
        app = web.Application()
        app.router.add_get("/", handle)
        runner = web.AppRunner(app, handler_cancellation=False, keepalive_timeout=0.1)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
            await site.start()
            server = cast(asyncio.Server, site._server)
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=client_tls, server_hostname="witness.internal"
            )
            try:
                writer.write(initial_request)
                await writer.drain()
                if complete:
                    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
                    assert head.startswith(b"HTTP/1.1 200 ")
                    assert await asyncio.wait_for(reader.readexactly(2), timeout=2) == b"ok"
                assert await asyncio.wait_for(reader.read(), timeout=2) == b""
                assert handled == (["/"] if complete else [])
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

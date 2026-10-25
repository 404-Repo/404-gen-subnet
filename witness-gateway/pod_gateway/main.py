"""Gateway process entry point; requires a selected, exactly identified Pod."""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
from collections.abc import AsyncIterator
from contextlib import suppress

import httpx
from aiohttp import web

from .admission import Admission
from .auth import Authenticator, Membership, MembershipRefresher
from .control import DrainControl, Registry
from .lifecycle import Allocator
from .models import PodSpec
from .runpod import RunPodClient
from .selection import Selection
from .server import Gateway
from .settings import GatewaySettings
from .state import R2StateStore


async def application(settings: GatewaySettings, worker_spec: PodSpec) -> web.Application:
    state = R2StateStore.connect(
        endpoint=settings.r2_endpoint.get_secret_value(),
        access_key=settings.r2_access_key_id.get_secret_value(),
        secret_key=settings.r2_secret_access_key.get_secret_value(),
        bucket=settings.r2_bucket,
    )
    registry = Registry()
    http = RunPodClient.http_client()
    provider = RunPodClient(
        settings.runpod_api_key.get_secret_value(),
        http,
        min_interval=settings.min_request_interval,
        cache_seconds=2,
    )
    admission = Admission(max_pods=settings.max_pods)
    allocator = Allocator(
        provider,
        DrainControl(registry),
        worker_spec,
        admission=admission,
        selection=Selection(state),
    )
    membership = Membership(
        settings.deployment,
        settings.netuid,
        settings.round,
        settings.test_hotkey,
    )
    rpc = httpx.Client(timeout=45, follow_redirects=False)
    refresher = MembershipRefresher(membership, rpc, settings.subtensor_rpc_url)

    def fence() -> bool:
        return allocator.selected(
            settings.gateway_scope,
            settings.start_id,
            settings.pod_id,
        )

    gateway = Gateway(
        allocator=allocator,
        auth=Authenticator(membership),
        registry=registry,
        deployment=settings.deployment,
        round_number=settings.round,
        round_end=settings.round_end,
        secret=settings.secret,
        proof_hash=hashlib.sha256((settings.parameters / "verifying.bin").read_bytes()).hexdigest(),
        fence=fence,
        pricing_hash=hashlib.sha256(settings.pricing_path.read_bytes()).hexdigest(),
    )
    app = gateway.application()

    async def lifecycle(_: web.Application) -> AsyncIterator[None]:
        async def refresh() -> None:
            while True:
                try:
                    await asyncio.to_thread(refresher.refresh)
                except Exception as error:  # noqa: BLE001 - retain last complete snapshot
                    print(
                        json.dumps(
                            {
                                "event": "membership_refresh_failed",
                                "error_type": type(error).__name__,
                            }
                        ),
                        flush=True,
                    )
                await asyncio.sleep(600)

        task = asyncio.create_task(refresh())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await registry.close()
            http.close()
            rpc.close()

    app.cleanup_ctx.append(lifecycle)
    return app


def run(settings: GatewaySettings, worker_spec: PodSpec, tls: ssl.SSLContext) -> None:
    web.run_app(
        application(settings, worker_spec),
        host="0.0.0.0",
        port=settings.port,
        ssl_context=tls,
        handler_cancellation=False,
    )

"""Gateway process entry point; requires a selected, exactly identified Pod."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import suppress

import httpx
from aiohttp import web

from .admission import Admission
from .auth import Authenticator, Membership, MembershipRefresher
from .control import DrainControl, Registry
from .lifecycle import Allocator
from .models import Journal
from .records import CallRecords
from .runpod import RunPodClient
from .server import Gateway
from .settings import Settings


async def application(settings: Settings) -> web.Application:
    state = settings.state()
    registry = Registry()
    http = RunPodClient.http_client()
    provider = RunPodClient(
        settings.env["GATEWAY_RUNPOD_API_KEY"],
        http,
        min_interval=float(settings.env.get("GATEWAY_MIN_REQUEST_INTERVAL", "1.1")),
    )
    admission = Admission(
        state,
        max_pods=int(settings.env.get("WITNESS_MAX_PODS", "250")),
        hourly_ceiling=str(settings.worker_budget),
        per_pod_ceiling=str(settings.pod_ceiling),
    )
    allocator = Allocator(
        state,
        provider,
        DrainControl(state, settings.secret, registry),
        settings.worker_spec(),
        admission=admission,
    )
    membership = Membership(
        state,
        settings.deployment,
        int(settings.env.get("WITNESS_NETUID", "17")),
        settings.round,
        settings.env.get("WITNESS_TEST_HOTKEY"),
    )
    rpc = httpx.Client(timeout=45, follow_redirects=False)
    refresher = MembershipRefresher(membership, rpc, settings.env["SUBTENSOR_RPC_URL"])

    def fence() -> bool:
        saved = state.load(settings.gateway_scope.key)
        if saved is None:
            return False
        journal = Journal.decode(saved.data, settings.gateway_scope)
        current = journal.session(journal.current)
        return (
            current.phase == "allocated"
            and current.start_id == settings.env["WITNESS_START_ID"]
            and current.pod_id == settings.env["RUNPOD_POD_ID"]
        )

    parameters = settings.env.get(
        "WITNESS_PARAMETERS", "/app/witness-server/verifier/params"
    )
    from pathlib import Path

    gateway = Gateway(
        allocator=allocator,
        records=CallRecords(state),
        auth=Authenticator(membership),
        registry=registry,
        evidence=state,
        deployment=settings.deployment,
        round_number=settings.round,
        round_end=settings.round_end,
        secret=settings.secret,
        proof_hash=hashlib.sha256(
            (Path(parameters) / "verifying.bin").read_bytes()
        ).hexdigest(),
        prompts=settings.prompts,
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
            for bridge in list(gateway.bridges.values()):
                await bridge.worker.close()
            http.close()
            rpc.close()

    app.cleanup_ctx.append(lifecycle)
    return app


def run(settings: Settings) -> None:
    web.run_app(
        application(settings),
        host="0.0.0.0",
        port=int(settings.env.get("GATEWAY_PORT", "8443")),
        ssl_context=settings.tls(),
        handler_cancellation=False,
    )

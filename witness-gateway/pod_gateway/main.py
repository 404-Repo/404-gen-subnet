"""Gateway process entry point; requires a selected, exactly identified Pod."""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from collections.abc import AsyncIterator

import httpx
from aiohttp import web
from subnet_common.github import GitHubClient
from subnet_common.witness.auth import Authenticator, AuthRejected
from subnet_common.witness.models import PodSpec
from subnet_common.witness.rounds import repository
from subnet_common.witness.selection import Selection
from subnet_common.witness.state import R2StateStore

from pod_gateway.membership import Membership, MembershipRefresher

from .admission import Admission
from .control import DrainControl, Registry
from .dependencies import worker_spec as resolved_worker_spec
from .diagnostics import output
from .lifecycle import Allocator
from .rounds import RoundController, RoundImages, download_image
from .runpod import RunPodClient
from .server import Gateway
from .settings import GatewaySettings


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
        settings.netuid,
        "unconfigured",
    )
    rpc = httpx.Client(timeout=45, follow_redirects=False)
    refresher = MembershipRefresher(membership, rpc, settings.subtensor_rpc_url)
    source_http = httpx.AsyncClient(timeout=30, trust_env=False)
    github = GitHubClient(
        repository(settings.competition_repo),
        token=settings.github_token.get_secret_value(),
    )

    async def head() -> tuple[int, float]:
        return await asyncio.to_thread(refresher.generation_head)

    rounds = RoundController(
        git=github, repo=settings.competition_repo, branch=settings.competition_branch,
        objects=RoundImages(state), pins=state, download=lambda url: download_image(source_http, url),
        head=head, ttl=settings.round_policy_seconds,
    )

    def fence() -> bool:
        pod = Allocator.winner(Allocator.owned(provider.list(fresh=True), settings.gateway_scope))
        return (pod is not None and pod.lifecycle == "active" and pod.status == "RUNNING" and
                pod.id == settings.pod_id and pod.markers["WITNESS_START_ID"] == settings.start_id)

    def authorize(hotkey: str) -> None:
        if settings.mode == "test":
            if hotkey != settings.test_hotkey:
                raise AuthRejected("hotkey_not_admitted", 403)
        else:
            membership.require(hotkey)

    gateway = Gateway(
        allocator=allocator,
        auth=Authenticator(authorize),
        registry=registry,
        deployment=settings.deployment,
        round_number="unconfigured",
        round_end=None,
        secret=settings.secret,
        proof_hash=hashlib.sha256((settings.parameters / "verifying.bin").read_bytes()).hexdigest(),
        fence=fence,
        create_interval=settings.create_interval,
        request_interval=settings.request_interval,
        poll_seconds=settings.poll_seconds,
        max_requests=settings.max_requests,
        max_queue=settings.max_queue,
        rounds=rounds,
        spec_for_round=lambda inputs: resolved_worker_spec(settings, inputs),
    )
    app = gateway.application()

    async def lifecycle(_: web.Application) -> AsyncIterator[None]:
        await github.__aenter__()
        github.client.headers["Cache-Control"] = "no-cache"
        async def refresh_round() -> None:
            while True:
                try:
                    await rounds.refresh()
                    await gateway.round_changed()
                except Exception as error:  # noqa: BLE001 - never log privileged diagnostics
                    output.write("round_refresh_failed", error_type=type(error).__name__)
                await asyncio.sleep(settings.round_refresh_seconds)

        async def refresh() -> None:
            while True:
                try:
                    await asyncio.to_thread(refresher.refresh)
                except Exception as error:  # noqa: BLE001 - retain last complete snapshot
                    output.write("membership_refresh_failed", error_type=type(error).__name__)
                await asyncio.sleep(600)

        tasks = []
        if settings.mode == "competition":
            tasks.append(asyncio.create_task(refresh()))
        tasks.append(asyncio.create_task(refresh_round()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await registry.close()
            http.close()
            rpc.close()
            await source_http.aclose()
            await github.__aexit__(None, None, None)

    app.cleanup_ctx.insert(0, lifecycle)
    return app


def run(settings: GatewaySettings, worker_spec: PodSpec, tls: ssl.SSLContext) -> None:
    web.run_app(
        application(settings, worker_spec),
        host="0.0.0.0",
        port=settings.port,
        ssl_context=tls,
        handler_cancellation=False,
    )

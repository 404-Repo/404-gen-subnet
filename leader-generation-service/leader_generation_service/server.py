"""Scoped job credentials behind the service's operator-managed HTTPS ingress."""

import asyncio
import base64
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict

from aiohttp import web
from bittensor_wallet import Wallet
from loguru import logger
from subnet_common.witness.rounds import repository

from .models import Job
from .round_runner import RoundRunner
from .runpod import read_config
from .settings import Settings


def application(controller: RoundRunner, settings: Settings) -> web.Application:
    dependencies, shutdown = controller.dependencies, controller.shutdown

    def authorize(request: web.Request, *, control: bool = False) -> Job:
        job = dependencies.store.load(int(request.match_info["number"]))
        if job is None:
            raise web.HTTPNotFound()
        if (
            job.snapshot.hotkey != dependencies.hotkey
            or job.snapshot.inputs.repository != repository(settings.github_repo)
            or job.snapshot.inputs.branch != settings.github_branch
            or job.snapshot.witness_url != settings.witness_url
        ):
            raise web.HTTPNotFound()
        if control:
            token = job.control_token
        else:
            if (
                job.published
                or job.stopped
                or job.allocation is None
                or job.allocation.phase in ("intent", "terminated")
            ):
                raise web.HTTPNotFound()
            token = job.allocation.token
        if token is None:
            raise web.HTTPNotFound()
        expected = f"Bearer {token}".encode()
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected):
            raise web.HTTPUnauthorized()
        return job

    def identity(job: Job) -> str:
        wallet = Wallet(
            name=job.snapshot.wallet_name,
            hotkey=job.snapshot.wallet_hotkey,
            path=str(settings.wallet_path),
        )
        if wallet.hotkey_file.is_encrypted() or wallet.hotkey.ss58_address != job.snapshot.hotkey:
            raise ValueError("Operator hotkey differs from the frozen job")
        path = settings.wallet_path / job.snapshot.wallet_name / "hotkeys" / job.snapshot.wallet_hotkey
        return base64.b64encode(path.read_bytes()).decode()

    async def control(request: web.Request) -> dict[str, object]:
        authorize(request, control=True)
        async with dependencies.lifecycle:
            job = authorize(request, control=True)
            action = request.match_info["resource"]
            if action in ("start", "resume"):
                if shutdown.should_stop:
                    raise web.HTTPServiceUnavailable()
                if action == "resume" and not job.published and not await controller.may_generate(job):
                    raise web.HTTPConflict()
                job.stopped = False
                dependencies.store.save(job)
                if action == "resume":
                    await dependencies.runner.terminate(job)
                await controller.run(job)
            elif action == "terminate":
                job.stopped = True
                dependencies.store.save(job)
                await dependencies.runner.terminate(job)
            status = await dependencies.runner.status(job)
            return {**status, "stopped": job.stopped, "published": job.published}

    async def respond(request: web.Request) -> web.StreamResponse:
        headers = {"Cache-Control": "no-store"}
        try:
            resource = request.match_info["resource"]
            if resource in ("start", "resume", "status", "terminate"):
                return web.json_response(await control(request), headers=headers)
            job = authorize(request)
            if resource == "job":
                return web.json_response(asdict(read_config(dependencies.store, job)), headers=headers)
            if resource == "identity":
                data = {"hotkey": await asyncio.to_thread(identity, job)}
            else:
                credentials = await dependencies.credentials.current()
                if credentials.account_id != job.snapshot.account_id:
                    raise ValueError("Provider account differs from the frozen job")
                data = {"access_token": credentials.access_token, "account_id": credentials.account_id}
            authorize(request)
            return web.json_response(data, headers=headers)
        except web.HTTPException as error:
            return web.json_response({"error": "job_access_denied"}, status=error.status, headers=headers)
        except Exception as error:
            logger.error("Job credential request failed ({})", type(error).__name__)
            return web.json_response({"error": "job_credentials_unavailable"}, status=503, headers=headers)

    app = web.Application(client_max_size=1024)
    app.router.add_get(r"/v1/jobs/{number:\d+}/{resource:identity|credentials|job}", respond, allow_head=False)
    app.router.add_get(r"/v1/jobs/{number:\d+}/{resource:status}", respond, allow_head=False)
    app.router.add_post(r"/v1/jobs/{number:\d+}/{resource:start|resume|terminate}", respond)
    return app


@asynccontextmanager
async def serve_credentials(controller: RoundRunner, settings: Settings) -> AsyncIterator[None]:
    runner = web.AppRunner(application(controller, settings), access_log=None, shutdown_timeout=5)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", settings.credentials_port).start()
        yield
    finally:
        try:
            await runner.cleanup()
        finally:
            async with controller.dependencies.lifecycle:
                pass

"""Local controls for the current generation job."""

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiohttp import web
from loguru import logger
from subnet_common.witness.rounds import repository

from .models import Job
from .round_runner import RoundRunner
from .settings import Settings


def application(controller: RoundRunner, settings: Settings) -> web.Application:
    dependencies, shutdown = controller.dependencies, controller.shutdown

    def authorize(request: web.Request) -> Job:
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
        token = job.control_token
        if token is None:
            raise web.HTTPNotFound()
        expected = f"Bearer {token}".encode()
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected):
            raise web.HTTPUnauthorized()
        return job

    async def control(request: web.Request) -> dict[str, object]:
        authorize(request)
        async with dependencies.lifecycle:
            job = authorize(request)
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
            return web.json_response(await control(request), headers=headers)
        except web.HTTPException as error:
            return web.json_response({"error": "job_access_denied"}, status=error.status, headers=headers)
        except Exception as error:
            logger.error("Job control request failed ({})", type(error).__name__)
            return web.json_response({"error": "job_credentials_unavailable"}, status=503, headers=headers)

    app = web.Application(client_max_size=1024)
    app.router.add_get(r"/v1/jobs/{number:\d+}/{resource:status}", respond, allow_head=False)
    app.router.add_post(r"/v1/jobs/{number:\d+}/{resource:start|resume|terminate}", respond)
    return app


@asynccontextmanager
async def serve_control(controller: RoundRunner, settings: Settings) -> AsyncIterator[None]:
    runner = web.AppRunner(application(controller, settings), access_log=None, shutdown_timeout=5)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", settings.control_port).start()
        yield
    finally:
        try:
            await runner.cleanup()
        finally:
            async with controller.dependencies.lifecycle:
                pass

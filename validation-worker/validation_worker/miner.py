"""Credential-free entry point, executed only inside the miner sandbox."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import inspect
import json
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, Self, Unpack, cast, overload

from openai.types.responses import Response, ResponseStreamEvent
from openai.types.responses.response_create_params import ResponseCreateParamsBase
from pydantic import BaseModel
from renderer_client import RenderOptions, RenderRejected
from renderer_client.client import DEFAULT_OPTIONS, RenderRequest
from witness_client import AsyncWitnessClient, CapturedResponse, WitnessClient, WitnessError
from witness_client.calls import RequestKey
from witness_client.response import AsyncModelStream, ModelStream, model_error, model_result


# Keep the protocol pipe separate from Python and native miner output.
wire = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
sys.stdout = sys.stderr
lock = threading.Lock()


def exchange(**fields: object) -> str:
    with lock:
        wire.write(json.dumps(fields, separators=(",", ":")) + "\n")
        wire.flush()
        return sys.stdin.readline()


class ModelReply(BaseModel):
    exchange_id: str
    status: int
    body: str


class RenderReply(BaseModel):
    png: str = ""
    rejection: str | None = None


class ReplayMismatch(WitnessError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="replay_mismatch")


class Witness(WitnessClient):
    """Responses adapter using the credential-free validation host bridge."""

    def __init__(self, provider: Literal["api", "codex"]) -> None:
        self._mode = provider
        self.responses = self
        self._renderer: Renderer | None = None

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        if self._renderer is None:
            raise ReplayMismatch("Execution host did not supply a renderer")
        return self._renderer.render(source, options)

    def read(self, key: RequestKey, body: bytes) -> CapturedResponse:
        reply = ModelReply.model_validate_json(
            exchange(
                operation="model",
                body=base64.b64encode(body).decode("ascii"),
                request_id=key.request_id,
                session_id=key.session_id,
            )
        )
        return CapturedResponse(reply.exchange_id, reply.status, base64.b64decode(reply.body, validate=True))

    @overload
    def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[False] = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response: ...

    @overload
    def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[True],
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> ModelStream: ...

    def create(
        self,
        *,
        request_id: int = 0,
        stream: bool = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response | ModelStream:
        """Repeat the same request and request_id to reuse its saved result.

        Change request_id for another model call with the same request.
        """
        key, body = self._request(request_id, session_id, params)
        try:
            captured = self.read(key, body)
        except ReplayMismatch:
            raise
        except WitnessError as error:
            raise model_error(error, self._mode) from error
        return model_result(captured.status, captured.body, self._mode, stream=stream)

    def close(self) -> None:
        pass

    def resume(self, exchange_id: str) -> Response:
        raise ReplayMismatch("Execution code must use responses.create()")

    def get_submitted(self) -> dict[str, dict[str, object]]:
        raise ReplayMismatch("Submission lookup belongs to the live runner")

    def submit(self, image: bytes, source: str | None) -> dict[str, object]:
        raise ReplayMismatch("Execution code must return its result; the live runner submits it")


class _AsyncStream(AsyncModelStream):
    async def __anext__(self) -> ResponseStreamEvent:
        try:
            event = await asyncio.to_thread(next, self._stream, None)
        except BaseException:
            await self.aclose()
            raise
        if event is None:
            raise StopAsyncIteration
        return event

    async def aclose(self) -> None:
        await asyncio.to_thread(self._stream.close)


class AsyncWitness(AsyncWitnessClient):
    def __init__(self, witness: Witness) -> None:
        self._witness = witness
        self.responses = self

    @overload
    async def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[False] = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response: ...

    @overload
    async def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[True],
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> AsyncModelStream: ...

    async def create(
        self,
        *,
        request_id: int = 0,
        stream: bool = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response | AsyncModelStream:
        if stream:
            result = await asyncio.to_thread(
                lambda: self._witness.create(
                    request_id=request_id,
                    stream=True,
                    session_id=session_id,
                    **params,
                ),
            )
            return _AsyncStream(result)
        return await asyncio.to_thread(
            lambda: self._witness.create(
                request_id=request_id,
                stream=False,
                session_id=session_id,
                **params,
            ),
        )

    async def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        return await asyncio.to_thread(self._witness.render, source, options)

    async def resume(self, exchange_id: str) -> Response:
        return self._witness.resume(exchange_id)

    async def get_submitted(self) -> dict[str, dict[str, object]]:
        return self._witness.get_submitted()

    async def submit(self, image: bytes, source: str | None) -> dict[str, object]:
        return self._witness.submit(image, source)

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self


class Renderer:
    renderer_id = "recorded"

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        RenderRequest.from_source(self.renderer_id, source, options)
        reply = RenderReply.model_validate_json(exchange(operation="render", source=source, query=options.query()))
        if reply.rejection is not None:
            raise RenderRejected(reply.rejection)
        return base64.b64decode(reply.png, validate=True)


def invoke_generate(
    generate: (
        Callable[[bytes, int, WitnessClient, Renderer], str | None]
        | Callable[[bytes, int, AsyncWitnessClient], Awaitable[str | None]]
    ),
    image: bytes,
    seed: int,
    witness: Witness,
    renderer: Renderer,
) -> str | None:
    witness._renderer = renderer
    if inspect.iscoroutinefunction(generate):

        async def run() -> str | None:
            entry = cast(Callable[[bytes, int, AsyncWitnessClient], Awaitable[str | None]], generate)
            return await entry(image, seed, AsyncWitness(witness))

        return asyncio.run(run())
    return cast(Callable[[bytes, int, WitnessClient, Renderer], str | None], generate)(image, seed, witness, renderer)


def main(provider: Literal["api", "codex"]) -> None:
    image = Path("/prompt.png").read_bytes()
    seed = int(sys.argv[1])
    spec = importlib.util.spec_from_file_location("submission", "/source/main.py")
    if spec is None or spec.loader is None:
        raise ValueError("Submission has no main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, "/source")
    spec.loader.exec_module(module)
    witness = Witness(provider)
    output = invoke_generate(module.generate, image, seed, witness, Renderer())
    if output is not None and not isinstance(output, str):
        raise ValueError("generate() must return JavaScript or None")
    wire.write(json.dumps({"operation": "result", "output": output}) + "\n")
    wire.flush()


if __name__ == "__main__":
    try:
        if sys.argv[2] not in ("api", "codex"):
            raise ValueError("Unknown provider")
        main("api" if sys.argv[2] == "api" else "codex")
    except Exception as error:
        wire.write(json.dumps({"operation": "result", "error": type(error).__name__}) + "\n")
        wire.flush()

"""Awaitable Responses calls with native async Pod and TLS transport."""

from __future__ import annotations

import asyncio
import hashlib
import os
import ssl
import time
import uuid
from contextlib import AsyncExitStack, nullcontext
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Self, Unpack, cast, overload

import httpx
from bittensor_wallet import Keypair
from openai.types.responses import Response
from openai.types.responses.response_create_params import ResponseCreateParamsBase
from renderer_client import RenderOptions, RenderUnavailable
from renderer_client.async_client import _render_response
from renderer_client.client import DEFAULT_OPTIONS, _source_bytes
from websockets.exceptions import WebSocketException

from . import request as openai_request
from .async_pods import AsyncPodGatewayClient
from .async_transport import async_exchange
from .async_tunnel import AsyncWebSocketTunnel
from .calls import RequestKey
from .client import (
    CaptureContext,
    CapturedResponse,
    _ClientState,
    _disclosure_claim,
    _encode_json,
    _submission_body,
    _submission_result,
    _submissions_result,
)
from .errors import ExchangePending, WitnessError
from .proof import Prover
from .response import AsyncModelStream, ModelStream, model_error, model_result
from .timing import identify_exchange, measure
from .types import Claim, EarlyDisclosure, HandshakeDisclosure, Session, WitnessConfig

if TYPE_CHECKING:
    from ._tls import TLSAbruptCloseError
else:
    from tlslite.errors import TLSAbruptCloseError


class AsyncWitnessClient(_ClientState):
    """Async Responses calls with the same durable call identities and recovery."""

    def __init__(
        self,
        config: WitnessConfig | str | os.PathLike[str],
        *,
        hotkey: Keypair,
        openai_api_key: str | None = None,
        subscription: Literal["codex"] | None = None,
        delivery_timeout: float = 600,
        parallel_calls: int = 3,
        parallel_preparations: int = 3,
    ) -> None:
        super().__init__(
            config,
            hotkey=hotkey,
            openai_api_key=openai_api_key,
            subscription=subscription,
            delivery_timeout=delivery_timeout,
            parallel_calls=parallel_calls,
            parallel_preparations=parallel_preparations,
        )
        self._prover: Prover | None = None
        self._prover_lock = asyncio.Lock()
        self._capture_slots = asyncio.BoundedSemaphore(parallel_calls)
        self._preparation = asyncio.BoundedSemaphore(parallel_preparations)
        self._calls: dict[str, asyncio.Lock] = {}
        self._http = httpx.AsyncClient(
            base_url=self._config["witness_url"].rstrip("/"),
            verify=ssl.create_default_context(),
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        )
        self._gateway = AsyncPodGatewayClient(self._http, hotkey, self._state, delivery_timeout)
        self.responses = self

    async def _get_prover(self) -> Prover:
        async with self._prover_lock:
            if self._prover is None:
                self._prover = await asyncio.to_thread(Prover, self._parameters)
            return self._prover

    async def aclose(self) -> None:
        """Close HTTP resources; retained state remains available for recovery."""
        try:
            await self._gateway.aclose()
        finally:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        try:
            await self._get_prover()
        except BaseException:
            await self.aclose()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def capture_context(self) -> CaptureContext:
        """Metadata for async hosts using capture and recovery."""
        await self._gateway.ensure_pod()
        assert self._gateway.round_number is not None
        namespace = hashlib.sha256(
            f"{self._http.base_url}/{self._hotkey.ss58_address}/"
            f"{self._gateway.round_number}/{self._mode}".encode()
        ).hexdigest()
        return CaptureContext(namespace, self._mode, self._state, self._gateway.delivery_timeout)

    async def replace_pod(self) -> None:
        """Replace the assigned Pod, or join an allocation already in progress.

        Creates one Pod if none exists and waits for its direct connection grant.
        Call from the live runner, outside generate() and validator replay, after
        stopping work on the current Pod. Durable calls and results are retained.
        """
        await self._gateway.replace_pod()

    async def capture_response(self, key: RequestKey, body: bytes) -> CapturedResponse:
        return await self._resolve(key, body, key.session_id)

    async def saved_response(self, exchange_id: str) -> CapturedResponse:
        """Retrieve and verify retained Pod evidence without issuing inference."""
        return await self._recover(exchange_id)

    def renderer_transport(self) -> httpx.AsyncBaseTransport:
        """Authenticate AsyncRendererClient requests to the assigned Pod."""
        return httpx.MockTransport(self._gateway.send)

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
        """Repeat a request and request_id to reuse or recover its saved result."""
        with measure("create"):
            key, body = self._request(request_id, session_id, params)
            try:
                captured = await self._resolve(key, body, session_id)
            except WitnessError as error:
                raise model_error(error, self._mode) from error
            result = model_result(captured.status, captured.body, self._mode, stream=stream)
            return AsyncModelStream(result) if isinstance(result, ModelStream) else result

    async def render(
        self, source: str, options: RenderOptions = DEFAULT_OPTIONS
    ) -> bytes:
        """Render a preview through the owned Pod connection without retrying."""
        _source_bytes(source)
        request = self._http.build_request(
            "POST",
            "/v1/render/grid",
            params=options.query(),
            json={"source": source},
            timeout=240,
        )
        try:
            response = await self._gateway.send(request)
            try:
                return await _render_response(response)
            finally:
                await response.aclose()
        except httpx.HTTPError as error:
            raise RenderUnavailable(f"Renderer request failed: {error}") from error

    async def submit(self, image: bytes, source: str | None) -> dict[str, object]:
        """Submit the runner's returned result to the assigned Pod without retrying."""
        body = _submission_body(image, source)
        request = self._http.build_request("POST", "/v1/submissions", json=body, timeout=150)
        try:
            response = await self._gateway.send(request)
        except httpx.HTTPError as error:
            raise WitnessError(
                "Final submission transport failed; check submitted results before another pass",
                code="submission_unresolved",
                source="gateway",
            ) from error
        try:
            return _submission_result(response)
        finally:
            await response.aclose()

    async def get_submitted(self) -> dict[str, dict[str, object]]:
        """Read this miner's saved round results, keyed by official prompt ID."""
        request = self._http.build_request("GET", "/v1/submissions")
        try:
            response = await self._gateway.send(request)
        except httpx.HTTPError as error:
            raise WitnessError(
                "Could not retrieve submitted results",
                code="submission_lookup_failed", source="gateway",
            ) from error
        try:
            return _submissions_result(response)
        finally:
            await response.aclose()

    async def resume(self, exchange_id: str) -> Response:
        """Retrieve a verified Pod recording using the same hotkey and provider."""
        captured = await self._recover(exchange_id)
        return cast(Response, model_result(captured.status, captured.body, self._mode))

    async def _recover(self, exchange_id: str) -> CapturedResponse:
        prover = await self._get_prover()
        identifier = str(uuid.UUID(exchange_id))
        identify_exchange(identifier)
        deadline = time.monotonic() + self._gateway.delivery_timeout
        while True:
            snapshot = await self._gateway.snapshot(identifier, deadline=deadline)
            if (
                snapshot.call.exchange_id != identifier
                or snapshot.hotkey != self._hotkey.ss58_address
                or snapshot.call.request.provider != self._mode
            ):
                raise WitnessError("Witness recording differs", identifier, code="invalid_recording")
            if snapshot.call.manifest_sha256 is not None:
                _session, status, response = await self._gateway.recorded(
                    snapshot.call.request, snapshot.call, prover.verifying_key_sha256,
                )
                if snapshot.call.phase == "failed" and status == 200:
                    raise WitnessError("Unusable provider response", identifier, code="capture_failed", source="gateway")
                return CapturedResponse(identifier, status, response)
            if snapshot.call.phase == "failed":
                raise WitnessError(
                    "Recorded witness capture failed", identifier, code="capture_failed", source="gateway",
                )
            if time.monotonic() >= deadline:
                raise ExchangePending("Capture remains unresolved", identifier)
            await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))

    async def _handshake(
        self,
        tunnel: AsyncWebSocketTunnel,
        disclosure: HandshakeDisclosure,
        *,
        deadline: float,
    ) -> None:
        """Verify TLS and its fixed HEAD probe before slow proof preparation."""
        raw = _encode_json(_disclosure_claim(disclosure))
        with measure("handshake_verification_wait"):
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("Capture wait timed out before handshake verification")
            await tunnel.verify(
                "handshake",
                raw,
                timeout,
                deadline=deadline,
            )

    async def _authorize(
        self,
        tunnel: AsyncWebSocketTunnel,
        disclosure: EarlyDisclosure,
        *,
        deadline: float,
    ) -> None:
        """Commit disclosure before the witness releases this one provider request."""
        if time.monotonic() >= deadline:
            raise TimeoutError("Capture wait timed out before credential proof")
        with measure("credential_proof"):
            prover = await self._get_prover()
            credential = await asyncio.to_thread(prover.prove, disclosure["private"])
        claim: Claim = {
            "credential": credential,
            **_disclosure_claim(disclosure),
        }
        with measure("claim_encode"):
            raw = _encode_json(claim)
        with measure("authorization_verification_wait"):
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("Capture wait timed out before authorization")
            await tunnel.verify(
                "authorize",
                raw,
                timeout,
                deadline=deadline,
            )

    async def _resolve(
        self, key: RequestKey, body: bytes, session_id: str | None
    ) -> CapturedResponse:
        lock = self._calls.setdefault(key.id, asyncio.Lock())
        async with lock:
            return await self._capture(key, body, session_id)

    async def _capture(
        self, key: RequestKey, body: bytes, session_id: str | None
    ) -> CapturedResponse:
        prover = await self._get_prover()
        identifier: str | None = None
        retries = 0
        raw_response: bytes | None = None
        try:
            snapshot = await self._gateway.start(key)
            if snapshot.call.phase == "failed" and (
                snapshot.call.authorized or not snapshot.call.not_dispatched
            ):
                snapshot = await self._gateway.start(key, expected_exchange_id=snapshot.call.exchange_id)
            identifier = snapshot.call.exchange_id
            identify_exchange(identifier)
            if snapshot.call.request != key or snapshot.hotkey != self._hotkey.ss58_address:
                raise WitnessError("Witness call differs", identifier, code="invalid_recording")
            attempted = False
            delivery_deadline = time.monotonic() + self._gateway.delivery_timeout
            deadline = min(
                delivery_deadline, time.monotonic() + snapshot.call.deadline - time.time()
            )
            needs_capture = snapshot.call.phase not in ("issued", "failed") or (
                snapshot.call.phase == "failed"
                and snapshot.call.not_dispatched
                and not snapshot.call.authorized
            )
            async with (
                self._capture_slots if needs_capture else nullcontext(),
                AsyncExitStack() as preparation,
            ):
                # Bound live preparation before opening provider connections.
                # Release its slot after authorization so inference can overlap.
                if snapshot.call.phase in ("reserved", "assigned"):
                    await preparation.enter_async_context(self._preparation)
                while snapshot.call.phase != "issued":
                    if snapshot.call.phase == "failed" and (
                        snapshot.call.authorized or not snapshot.call.not_dispatched
                    ):
                        break
                    if (
                        snapshot.call.phase == "failed"
                        and snapshot.call.not_dispatched
                        and not snapshot.call.authorized
                    ) or (snapshot.call.phase == "assigned" and attempted):
                        expected_exchange_id = None
                        if snapshot.call.phase == "failed":
                            if retries >= 2:
                                break
                            retries += 1
                            deadline = delivery_deadline
                            expected_exchange_id = snapshot.call.exchange_id
                            await asyncio.sleep(min(retries, max(0, deadline - time.monotonic())))
                        if time.monotonic() >= deadline:
                            raise WitnessError(
                                "Capture wait timed out",
                                identifier,
                                code="capture_timeout",
                                source="gateway",
                            )
                        snapshot = await self._gateway.start(
                            key, expected_exchange_id=expected_exchange_id, deadline=deadline
                        )
                        if (
                            snapshot.call.request != key
                            or snapshot.hotkey != self._hotkey.ss58_address
                        ):
                            raise WitnessError("Witness retry differs", code="invalid_recording")
                        identifier = snapshot.call.exchange_id
                        identify_exchange(identifier)
                        deadline = min(
                            delivery_deadline,
                            time.monotonic() + snapshot.call.deadline - time.time(),
                        )
                        attempted = False
                        raw_response = None
                        await preparation.aclose()
                        if snapshot.call.phase in ("reserved", "assigned"):
                            await preparation.enter_async_context(self._preparation)
                        continue
                    if not attempted and snapshot.call.phase != "failed":
                        if snapshot.session is not None:
                            session = cast(Session, snapshot.session)
                            host, path = openai_request.PROVIDERS[self._mode]
                            if any(
                                session.get(k) != v
                                for k, v in {
                                    "id": identifier,
                                    "protocol": 2,
                                    "provider": f"https://{host}{path}",
                                    "proof_key_sha256": prover.verifying_key_sha256,
                                    "miner": self._hotkey.ss58_address,
                                }.items()
                            ):
                                raise WitnessError(
                                    "Witness session differs",
                                    identifier,
                                    code="invalid_recording",
                                )
                            attempted = True
                            request = await asyncio.to_thread(
                                openai_request.prepare_request,
                                self._config,
                                self._mode,
                                self._api_key,
                                body=body,
                                session_id=session_id,
                            )
                            try:
                                async with self._gateway.tunnel(
                                    identifier, deadline=deadline
                                ) as tunnel:

                                    async def authorize(disclosure: EarlyDisclosure) -> None:
                                        await self._authorize(tunnel, disclosure, deadline=deadline)
                                        await preparation.aclose()

                                    raw_response = await async_exchange(
                                        tunnel,
                                        request.host,
                                        request.chunks,
                                        handshake=lambda disclosure: self._handshake(
                                            tunnel, disclosure, deadline=deadline
                                        ),
                                        authorize=authorize,
                                        timeout=max(0, deadline - time.monotonic()),
                                    )
                            except (
                                OSError,
                                TimeoutError,
                                WebSocketException,
                                WitnessError,
                                TLSAbruptCloseError,
                            ):
                                # Recover a claimed exchange that may already be paying/capturing.
                                snapshot = await self._gateway.snapshot(
                                    identifier, deadline=deadline
                                )
                    if snapshot.call.phase == "failed":
                        continue
                    if time.monotonic() >= deadline:
                        raise WitnessError(
                            "Capture wait timed out",
                            identifier,
                            code="capture_timeout",
                            source="gateway",
                        )
                    snapshot = await self._gateway.snapshot(identifier, deadline=deadline)
                    if snapshot.call.phase != "issued":
                        await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))
            if snapshot.call.phase == "failed" and snapshot.call.manifest_sha256 is None:
                raise WitnessError(
                    "Recorded witness capture failed",
                    identifier,
                    code="capture_failed",
                    source="gateway",
                )
            session, status, response = await self._gateway.recorded(
                key,
                snapshot.call,
                prover.verifying_key_sha256,
                request=body,
                raw_response=raw_response,
            )
            if snapshot.call.phase == "failed" and status == 200:
                raise WitnessError("Unusable provider response", identifier, code="capture_failed", source="gateway")
            return CapturedResponse(identifier, status, response)
        except WitnessError as error:
            error.exchange_id = error.exchange_id or identifier
            raise
        except httpx.TimeoutException as error:
            raise WitnessError(
                "Witness connection timed out", identifier, code="capture_timeout", source="gateway"
            ) from error
        except Exception as error:
            raise WitnessError(
                "Witness capture did not complete",
                identifier,
                code="capture_unresolved",
                source="gateway",
            ) from error

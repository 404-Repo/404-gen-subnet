"""Authenticated worker allocation and result recovery."""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

import httpx
from bittensor_wallet import Keypair
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from .auth import WITNESS_ID, hotkey_headers
from .delivery import request, retry
from .errors import WitnessError
from .state import save
from .tunnel import MAX_BYTES, WebSocketTunnel
from .types import AllocationStatus, ExchangeStatus, JSONObject, Session


def encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


class GatewayClient:
    """Queue witness workers and exchange data through the gateway.

    WitnessClient uses this to connect to its assigned worker and retrieve
    recorded evidence and responses. Gateway requests are signed with the
    miner's hotkey. Each ``identifier`` is the exchange UUID in Session's ``id``
    field.
    """

    def __init__(
        self,
        http: httpx.Client,
        hotkey: Keypair,
        state: Path,
        delivery_timeout: float,
    ) -> None:
        """Configure gateway access and local job storage.

        Args:
            http: HTTP client with the gateway base_url; the caller closes it.
            hotkey: Miner key used to sign gateway requests.
            state: Existing private directory for per-exchange recovery files.
            delivery_timeout: Retry window in seconds for each delivery phase.
        """
        self.http, self.hotkey = http, hotkey
        self.state, self.delivery_timeout = state, delivery_timeout

    def get(
        self,
        identifier: str,
        name: str,
        limit: int = MAX_BYTES,
        *,
        deadline: float | None = None,
    ) -> bytes | None:
        """Read exchange progress or a saved response, or None if it is not ready.

        ``name`` is relative to ``/exchanges/{identifier}/``, for example
        ``progress`` or ``response.sse``. Responses exceeding
        ``limit`` bytes raise WitnessError. Transient failures are retried using
        ``deadline``, an absolute time.monotonic() value; when omitted, the retry
        window is ``delivery_timeout`` seconds.
        """
        try:
            return self._request_with_retry(
                "GET", f"/exchanges/{identifier}/{name}", limit=limit, deadline=deadline
            )
        except httpx.TransportError as error:
            raise WitnessError(
                f"Gateway download failed: {error}", identifier, source="gateway"
            ) from error

    def release(self, identifier: str) -> None:
        """Retire an allocation; a worker already capturing OpenAI may still save evidence."""
        self._request_with_retry("POST", f"/release/{identifier}")

    def remaining_seconds(self, identifier: str) -> float:
        """Return seconds left before the exchange's job deadline.

        Read the gateway deadline from ``state/<identifier>/job.json``. It covers both
        queueing and execution. Raise WitnessError if the deadline has passed.
        """
        job: JSONObject = json.loads(
            (self.state / identifier / "job.json").read_bytes()
        )
        remaining = float(str(job.get("expires", job["deadline"]))) - time.time()
        if remaining <= 0:
            raise WitnessError(
                "Queued exchange deadline reached", identifier, source="gateway"
            )
        return remaining

    def queue_and_wait_for_worker(
        self, provider: Literal["api", "codex"], *, replace: str | None = None
    ) -> Session:
        """Queue one worker for an exchange and wait for its session metadata.

        ``provider`` selects the OpenAI API ("api") or ChatGPT subscription
        ("codex"). Save the signed job locally before queueing it. Poll allocation
        status every five seconds until the worker is ready or the round ends.
        Set replace to retire that existing allocation when queueing this one.

        Return the worker's Session, including its exchange ID and provider
        endpoint. The OpenAI request is sent later through ``tunnel()``. Queue
        or startup failures raise WitnessError carrying the exchange ID.
        """
        if provider not in ("api", "codex"):
            raise ValueError("Unknown witness provider")
        identifier = str(uuid.uuid4())
        job: JSONObject = {
            "operation": "exchange",
            "hotkey": self.hotkey.ss58_address,
            "receiver": WITNESS_ID,
            "exchange_id": identifier,
            "deadline": math.floor(time.time()) + 7 * 86400,
            "provider": provider,
        }
        if replace is not None:
            job["replace"] = str(uuid.UUID(replace))
        job["signature"] = self.hotkey.sign(
            b"404-witness-queue/v1\n" + encode(job)
        ).hex()
        directory = self.state / identifier
        directory.mkdir(mode=0o700)
        body = encode(job)
        save(directory / "job.json", body)
        deadline = time.monotonic() + self.delivery_timeout
        try:
            reply = self._request_with_retry("POST", "/run", body, deadline=deadline)
            job_reply = _parse_allocation(
                reply or b"{}", identifier, self.hotkey.ss58_address
            )
            job["expires"] = job_reply["deadline"]
            save(directory / "job.json", encode(job))
            deadline = time.monotonic() + self.remaining_seconds(identifier)
            while time.monotonic() < deadline:
                raw = self._request_with_retry(
                    "GET", f"/status/{identifier}", deadline=deadline
                )
                if time.monotonic() >= deadline:
                    break
                status = _parse_allocation(
                    raw or b"{}", identifier, self.hotkey.ss58_address
                )
                if status["queue_state"] in ("retired", "finished"):
                    raise WitnessError(
                        "Queued worker stopped before opening the exchange",
                        identifier,
                        source="gateway",
                    )
                if (
                    status["queue_state"] == "connected"
                    and status["session"] is not None
                ):
                    return status["session"]
                time.sleep(min(5, max(0, deadline - time.monotonic())))
            raise WitnessError(
                "Queued exchange deadline reached", identifier, source="gateway"
            )
        except Exception as error:
            if isinstance(error, WitnessError):
                error.exchange_id = error.exchange_id or identifier
                error.code = error.code or "queue_incomplete"
                raise
            raise WitnessError(
                f"Worker startup did not complete; no OpenAI request was sent. "
                f"Use release() with exchange {identifier} before queueing a replacement: {error}",
                identifier,
                code="queue_incomplete",
                source="gateway",
            ) from error

    def status(self, identifier: str) -> ExchangeStatus:
        """Fetch one snapshot of the worker's progress as ExchangeStatus.

        Missing progress returns {"status": "new"}. The "issued" status
        means evidence is saved, including when OpenAI rejected the request.
        The caller handles polling and retries.
        """
        raw = self._request_once("GET", f"/exchanges/{identifier}/progress")
        return _parse_status(raw, identifier) if raw is not None else {"status": "new"}

    def wait_for_evidence(self, identifier: str) -> ExchangeStatus:
        """Wait for the worker to save evidence and return its issued status.

        Poll every half second, retrying transient failures within
        delivery_timeout. Raise WitnessError if the exchange fails or evidence
        remains unavailable when the timeout expires.
        """
        deadline = time.monotonic() + self.delivery_timeout
        try:
            while True:
                value = retry(lambda: self.status(identifier), deadline=deadline)
                if value["status"] == "issued":
                    return value
                if value["status"] not in ("new", "running"):
                    raise WitnessError(
                        f"Exchange cannot be resumed: {value['status']}",
                        identifier,
                        source="gateway",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WitnessError(
                        "Witness is still preparing the capture",
                        identifier,
                        code="capture_in_progress",
                        source="gateway",
                    )
                time.sleep(min(remaining, 0.5))
        except WitnessError as error:
            error.exchange_id = identifier
            raise
        except httpx.TransportError as error:
            raise WitnessError(
                f"Evidence delivery failed; resume exchange {identifier}: {error}",
                identifier,
                source="gateway",
            ) from error

    def response(self, identifier: str) -> bytes:
        """Download the recorded OpenAI response body as bytes.

        Read response.sse after the witness has issued evidence. Streaming
        replies contain SSE events; provider errors may contain JSON instead.
        Raise WitnessError if the file is unavailable or the download fails.
        """
        try:
            raw = self.get(identifier, "response.sse")
            if raw is None:
                raise WitnessError(
                    "Recorded response is unavailable", identifier, source="gateway"
                )
            return raw
        except WitnessError as error:
            error.exchange_id = identifier
            raise
        except httpx.TransportError as error:
            raise WitnessError(
                f"Response download failed; resume exchange {identifier}: {error}",
                identifier,
                source="gateway",
            ) from error

    @contextmanager
    def tunnel(self, identifier: str) -> Iterator[WebSocketTunnel]:
        """Connect to the assigned worker for one TLS exchange, closing on exit."""
        path = f"/exchanges/{identifier}/connect"
        headers = hotkey_headers(self.hotkey, b"", method="GET", path=path)
        url = (
            str(self.http.base_url).rstrip("/").replace("https://", "wss://", 1) + path
        )
        try:
            with connect(
                url,
                additional_headers=headers,
                proxy=None,
                compression=None,
                max_size=256 * 1024,
                max_queue=16,
                open_timeout=30,
                close_timeout=5,
            ) as socket:
                yield WebSocketTunnel(socket, identifier)
        except (WebSocketException, OSError, TimeoutError) as error:
            raise WitnessError(
                "Worker connection lost; recover the result or release the allocation",
                identifier,
                code="connection_lost",
                source="gateway",
            ) from error

    def _request_once(
        self, method: str, path: str, body: bytes = b"", *, limit: int = 32768
    ) -> bytes | None:
        # Sign each attempt afresh. Retried jobs and objects keep the same bytes.
        headers = hotkey_headers(self.hotkey, body, method=method, path=path)
        try:
            return request(self.http, method, path, body, headers=headers, limit=limit)
        except WitnessError as error:
            if error.status_code == 404 and error.code == "object_not_ready":
                return None
            raise

    def _request_with_retry(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        limit: int = 32768,
        deadline: float | None = None,
    ) -> bytes | None:
        return retry(
            lambda: self._request_once(method, path, body, limit=limit),
            deadline=(
                deadline
                if deadline is not None
                else time.monotonic() + self.delivery_timeout
            ),
        )


def _json_object(raw: bytes, identifier: str) -> dict[str, object]:
    try:
        value: object = json.loads(raw)
    except ValueError as error:
        raise WitnessError(
            "Invalid gateway JSON",
            identifier,
            code="invalid_gateway_response",
            source="gateway",
        ) from error
    if not isinstance(value, dict):
        raise WitnessError(
            "Expected a gateway JSON object",
            identifier,
            code="invalid_gateway_response",
            source="gateway",
        )
    return value


def _parse_allocation(raw: bytes, identifier: str, hotkey: str) -> AllocationStatus:
    value = _json_object(raw, identifier)
    if (
        value.get("exchange_id") != identifier
        or value.get("hotkey") != hotkey
        or not isinstance(value.get("round"), str)
        or type(value.get("deadline")) is not int
        or value.get("queue_state")
        not in ("submitting", "queued", "uncertain", "connected", "finished", "retired")
        or "session" not in value
    ):
        raise WitnessError(
            "Invalid gateway allocation metadata",
            identifier,
            code="invalid_gateway_response",
            source="gateway",
        )
    if value["session"] is not None:
        value["session"] = _parse_session(value["session"], identifier)
    return cast(AllocationStatus, value)


def _parse_session(value: object, identifier: str) -> Session:
    if (
        not isinstance(value, dict)
        or type(value.get("protocol")) is not int
        or not all(
            isinstance(value.get(name), str)
            for name in ("id", "provider", "proof_key_sha256", "round", "miner")
        )
    ):
        raise WitnessError(
            "Invalid witness session metadata",
            identifier,
            code="invalid_gateway_response",
            source="gateway",
        )
    return cast(Session, value)


def _parse_status(raw: bytes, identifier: str) -> ExchangeStatus:
    value = _json_object(raw, identifier)
    if (
        not isinstance(value.get("status"), str)
        or any(
            name in value and type(value[name]) is not bool
            for name in ("handshook", "authorized")
        )
        or any(
            name in value and value[name] is not None and type(value[name]) is not int
            for name in ("provider_status", "evidence_expires")
        )
    ):
        raise WitnessError(
            "Invalid witness status metadata",
            identifier,
            code="invalid_gateway_response",
            source="gateway",
        )
    return cast(ExchangeStatus, value)

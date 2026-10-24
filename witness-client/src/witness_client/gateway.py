"""Authenticated witness transport and result recovery."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import httpx
from bittensor_wallet import Keypair
from websockets.client import ClientProtocol
from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from .auth import hotkey_headers
from .delivery import request, retry
from .errors import ExchangePending, WitnessError
from .timing import measure, timed
from .tunnel import MAX_BYTES, WebSocketTunnel
from .types import ExchangeStatus, JSONObject


class _GatewayConnection(ClientConnection):
    """Send the upgrade before reading TLS session tickets on the shared socket."""

    def __init__(
        self, sock: socket.socket, protocol: ClientProtocol, **kwargs: Any
    ) -> None:
        self._upgrade_sent = threading.Event()
        super().__init__(sock, protocol, **kwargs)

    def recv_events(self) -> None:
        # Concurrent ticket processing and SSL_write can lose the opening request.
        self._upgrade_sent.wait()
        super().recv_events()

    def send_data(self) -> None:
        try:
            super().send_data()
        finally:
            self._upgrade_sent.set()

    def close_socket(self) -> None:
        self._upgrade_sent.set()
        super().close_socket()


class GatewayClient:
    """Exchange data with witness workers through the gateway.

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
        preparation and execution. Raise WitnessError if the deadline has passed.
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
                    raise ExchangePending("Witness is still preparing the capture", identifier)
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

    @timed("recorded_response_download")
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
            with measure("websocket_connect"):
                socket = connect(
                    url,
                    additional_headers=headers,
                    proxy=None,
                    compression=None,
                    max_size=256 * 1024,
                    max_queue=16,
                    open_timeout=30,
                    close_timeout=5,
                    create_connection=_GatewayConnection,
                )
            with socket:
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

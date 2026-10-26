"""Send OpenAI requests through the subnet witness."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import ssl
import threading
import time
import uuid
import zlib
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Self, Unpack, cast, overload
from urllib.parse import urlsplit

import httpx
from bittensor_wallet import Keypair
from openai.types.responses import Response
from openai.types.responses.response_create_params import (
    ResponseCreateParamsBase,
    ResponseCreateParamsStreaming,
)
from websockets.exceptions import WebSocketException

from . import request as openai_request
from .calls import RequestKey
from .errors import ExchangePending, WitnessError
from .pods import PodGatewayClient
from .proof import Prover
from .response import ModelStream, model_result
from .state import save as _save
from .timing import identify_exchange, measure, timed
from .transport import exchange
from .tunnel import WebSocketTunnel
from .types import (
    Claim,
    EarlyDisclosure,
    HandshakeClaim,
    HandshakeDisclosure,
    Session,
    WitnessConfig,
)

if TYPE_CHECKING:
    from ._tls import TLSAbruptCloseError
else:
    from tlslite.errors import TLSAbruptCloseError


@dataclass(frozen=True)
class CaptureContext:
    """Local capture namespace, storage and delivery settings."""

    namespace: str
    provider: Literal["api", "codex"]
    state_directory: Path
    delivery_timeout: float


@dataclass(frozen=True)
class CapturedResponse:
    exchange_id: str
    status: int
    body: bytes


class WitnessClient:
    """Responses calls with durable allocation-scoped capture and recovery."""

    @timed("initialize")
    def __init__(
        self,
        config: WitnessConfig | str | os.PathLike[str],
        *,
        hotkey: Keypair,
        openai_api_key: str | None = None,
        subscription: Literal["codex"] | None = None,
        delivery_timeout: float = 600,
        parallel_preparations: int = 3,
        call_slots: threading.Semaphore | None = None,
    ) -> None:
        """Configure credentials, proof parameters, and local recovery storage.

        Args:
            config: WitnessConfig mapping or path to its JSON file. Requires
                witness_url (HTTPS origin), proof_parameters path, and pinned round.
                Set state_directory to keep recovery files on persistent
                storage; otherwise the user's local state directory is used.
            hotkey: Miner signing key loaded and managed by the caller.
            openai_api_key: OpenAI API key. Choose this or subscription.
            subscription: Use "codex" for an existing local Codex login.
                Credentials are refreshed when needed.
            delivery_timeout: Positive retry window in seconds for each gateway
                delivery phase (default 600), not a total create() time limit.
            parallel_preparations: Maximum concurrent Pod call preparations
                across calls (1 through 32, default 3).
        """
        self._config: WitnessConfig
        if isinstance(config, dict):
            self._config = config.copy()
        else:
            self._config = json.loads(Path(config).read_text(encoding="utf-8"))
        if self._config.get("transport", "pods-v1") != "pods-v1":
            raise ValueError(
                "Only witness Pods are supported; use a Pod gateway and omit 'transport'"
            )
        if (
            re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self._config.get("round", ""))
            is None
        ):
            raise ValueError("Pod transport requires a pinned competition round")
        if (
            not isinstance(parallel_preparations, int)
            or isinstance(parallel_preparations, bool)
            or not 1 <= parallel_preparations <= 32
        ):
            raise ValueError(
                "parallel_preparations must be an integer from 1 through 32"
            )
        if not math.isfinite(delivery_timeout) or delivery_timeout <= 0:
            raise ValueError("delivery_timeout must be finite and > 0 seconds")
        if (openai_api_key is None) == (subscription is None) or subscription not in (
            None,
            "codex",
        ):
            raise ValueError("Supply either openai_api_key or subscription='codex'")
        self._mode: Literal["api", "codex"] = (
            "codex" if subscription is not None else "api"
        )
        self._api_key = openai_api_key
        base = self._config["witness_url"].rstrip("/")
        url = urlsplit(base)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or url.username
        ):
            raise ValueError("Witness URL must be an HTTPS origin")
        state_directory = self._config.get("state_directory")
        if state_directory is not None:
            self._state = Path(state_directory).expanduser()
        else:
            state_home = Path(os.environ.get("XDG_STATE_HOME", ""))
            if not state_home.is_absolute():
                state_home = Path.home() / ".local" / "state"
            self._state = state_home / "404-witness-client"
        parameters = Path(self._config["proof_parameters"]).expanduser().resolve()

        self._state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._state.stat().st_mode & 0o077:
            raise ValueError("Witness state directory must be private (chmod 700)")
        with measure("prover_initialize"):
            self._prover = Prover(parameters)
        self._preparation = threading.BoundedSemaphore(parallel_preparations)
        self._call_slots = call_slots
        self._calls_lock = threading.Lock()
        self._calls: dict[str, threading.Lock] = {}
        self._hotkey = hotkey
        with measure("http_initialize"):
            context = ssl.create_default_context()
            self._http = httpx.Client(
                base_url=base,
                verify=context,
                timeout=30,
                follow_redirects=False,
                trust_env=False,
            )
        self._gateway = PodGatewayClient(
            self._http,
            self._hotkey,
            self._state,
            delivery_timeout,
            round_number=self._config["round"],
        )
        # Preserve the familiar Responses API spelling without an extra layer.
        self.responses = self

    @property
    def capture_context(self) -> CaptureContext:
        """Metadata for trusted callers using capture and recovery."""
        namespace = hashlib.sha256(
            f"{self._http.base_url}/{self._hotkey.ss58_address}/"
            f"{self._config['round']}/{self._mode}".encode()
        ).hexdigest()
        return CaptureContext(
            namespace=namespace,
            provider=self._mode,
            state_directory=self._state,
            delivery_timeout=self._gateway.delivery_timeout,
        )

    def capture_response(self, key: RequestKey, body: bytes) -> CapturedResponse:
        """Capture or recover the logical call identified by its canonical request."""
        return self._resolve(key, body, key.session_id)

    def saved_response(self, exchange_id: str) -> CapturedResponse:
        """Read retained capture bytes without issuing inference."""
        return self._saved_response(exchange_id)

    def close(self) -> None:
        """Close HTTP connections, keeping saved state available for recovery."""
        self._gateway.close()
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

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

    @timed("create")
    def create(
        self,
        *,
        request_id: int = 0,
        stream: bool = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response | ModelStream:
        key, body = self._request(request_id, session_id, params)
        captured = self._resolve(key, body, session_id)
        return model_result(captured.status, captured.body, self._mode, stream=stream)

    def _request(
        self, request_id: int, session_id: str | None, params: ResponseCreateParamsBase
    ) -> tuple[RequestKey, bytes]:
        openai_request.validate_session_id(session_id, self._mode)
        normalized = cast(
            ResponseCreateParamsStreaming,
            {"service_tier": "default", **params, "stream": True},
        )
        body = openai_request.serialize_body(normalized, self._mode)
        return RequestKey.from_body(
            self._mode, body, request_id, session_id=session_id
        ), body

    def release(self, exchange_id: str) -> None:
        """Release an unclaimed capture without issuing inference.

        This is safe to repeat.
        An OpenAI request already in progress may still finish and save evidence;
        use resume() to retrieve it. Releasing does not issue another GPT request.
        """
        self._gateway.release(str(uuid.UUID(exchange_id)))

    def renderer_transport(self) -> httpx.BaseTransport:
        """Authenticate RendererClient requests to this miner's assigned Pod."""
        return httpx.MockTransport(self._gateway.render)

    @timed("resume")
    def resume(self, exchange_id: str) -> Response:
        """Retrieve a recorded result without making another OpenAI request.

        Pass the exchange_id from WitnessError and reuse the original
        state_directory, hotkey, and authentication mode during the active
        access window. Local recovery files and retained witness evidence
        must still be available. Returns
        an OpenAI Response as create() does; raises WitnessError if the
        exchange is unavailable or unfinished; provider rejections use SDK errors.
        """
        identifier = str(uuid.UUID(exchange_id))
        identify_exchange(identifier)
        job = json.loads((self._state / identifier / "job.json").read_bytes())
        invocation = RequestKey.model_validate(job["request"])
        if invocation.provider != self._mode:
            raise WitnessError("Cannot resume a different provider", identifier)
        deadline = time.monotonic() + self._gateway.delivery_timeout
        while True:
            snapshot = self._gateway.snapshot(identifier)
            if snapshot.call.phase == "issued":
                saved_request = self._state / identifier / "request.json"
                request = None
                if saved_request.is_file():
                    with saved_request.open("rb") as local:
                        request = local.read(8 * 1024 * 1024 + 1)
                _session, recorded_status, response = self._gateway.recorded(
                    invocation,
                    snapshot.call,
                    self._prover.verifying_key_sha256,
                    request=request,
                )
                self._save_response(
                    CapturedResponse(identifier, recorded_status, response)
                )
                return cast(
                    Response, model_result(recorded_status, response, self._mode)
                )
            if (
                snapshot.call.phase == "failed"
                and snapshot.call.not_dispatched
                and not snapshot.call.authorized
            ):
                raise WitnessError(
                    "Recorded witness capture failed",
                    identifier,
                    code="capture_failed",
                    source="gateway",
                )
            if time.monotonic() >= deadline:
                raise ExchangePending("Capture remains unresolved", identifier)
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))

    def _handshake(
        self, tunnel: WebSocketTunnel, disclosure: HandshakeDisclosure
    ) -> None:
        """Verify TLS and its fixed HEAD probe before slow proof preparation."""
        claim = _disclosure_claim(disclosure)
        with measure("handshake_verification_wait"):
            tunnel.verify(
                "handshake",
                _encode_json(claim),
                min(
                    self._gateway.delivery_timeout,
                    self._gateway.remaining_seconds(tunnel.identifier),
                ),
            )

    def _authorize(self, tunnel: WebSocketTunnel, disclosure: EarlyDisclosure) -> None:
        """Commit disclosure before the witness releases this one provider request."""
        with measure("credential_proof"):
            credential = self._prover.prove(disclosure["private"])
        claim: Claim = {
            "credential": credential,
            **_disclosure_claim(disclosure),
        }
        with measure("claim_encode_and_save"):
            raw = _encode_json(claim)
            _save(self._state / tunnel.identifier / "claim.json", raw)
        with measure("authorization_verification_wait"):
            tunnel.verify(
                "authorize",
                raw,
                min(
                    self._gateway.delivery_timeout,
                    self._gateway.remaining_seconds(tunnel.identifier),
                ),
            )

    def _save_response(self, response: CapturedResponse) -> None:
        directory = self._state / response.exchange_id
        _save(directory / "response.sse", response.body)
        _save(
            directory / "outcome.json",
            _encode_json(
                {
                    "status": response.status,
                    "sha256": hashlib.sha256(response.body).hexdigest(),
                }
            ),
        )

    def _saved_response(self, exchange_id: str) -> CapturedResponse:
        directory = self._state / str(uuid.UUID(exchange_id))
        value = json.loads((directory / "outcome.json").read_bytes())
        body = (directory / "response.sse").read_bytes()
        if hashlib.sha256(body).hexdigest() != value["sha256"]:
            raise WitnessError(
                "Saved response changed", exchange_id, code="invalid_recording"
            )
        return CapturedResponse(exchange_id, value["status"], body)

    def _resolve(
        self, key: RequestKey, body: bytes, session_id: str | None
    ) -> CapturedResponse:
        with self._calls_lock:
            lock = self._calls.setdefault(key.id, threading.Lock())
        with lock:
            return self._capture(key, body, session_id)

    def _capture(
        self, key: RequestKey, body: bytes, session_id: str | None
    ) -> CapturedResponse:
        identifier: str | None = None
        retries = 0
        raw_response: bytes | None = None
        try:
            snapshot = self._gateway.reserve(key)
            identifier = snapshot.call.exchange_id
            identify_exchange(identifier)
            if (
                snapshot.call.request != key
                or snapshot.hotkey != self._hotkey.ss58_address
            ):
                raise WitnessError(
                    "Call reservation differs", identifier, code="invalid_recording"
                )
            directory = self._state / identifier
            directory.mkdir(mode=0o700, exist_ok=True)
            _save(
                directory / "job.json",
                _encode_json(
                    {
                        "deadline": snapshot.call.deadline,
                        "request": key.model_dump(),
                    }
                ),
            )
            attempted = False
            deadline = time.monotonic() + snapshot.call.deadline - time.time()
            with (
                self._call_slots
                if self._call_slots is not None
                and snapshot.call.phase not in ("issued", "failed")
                else nullcontext(),
                ExitStack() as preparation,
            ):
                # Bound live preparation before opening provider connections.
                # Release its slot after authorization so inference can overlap.
                if snapshot.call.phase in ("reserved", "assigned"):
                    preparation.enter_context(self._preparation)
                while snapshot.call.phase != "issued":
                    if snapshot.call.phase == "failed" and (
                        snapshot.call.authorized or not snapshot.call.not_dispatched
                    ):
                        break
                    if (
                        snapshot.call.phase == "failed"
                        and snapshot.call.not_dispatched
                        and not snapshot.call.authorized
                    ):
                        if retries >= 2:
                            break
                        retries += 1
                        time.sleep(retries)
                        snapshot = self._gateway.retry(key, snapshot.call.exchange_id)
                        if (
                            snapshot.call.request != key
                            or snapshot.hotkey != self._hotkey.ss58_address
                        ):
                            raise WitnessError(
                                "Retry reservation differs", code="invalid_recording"
                            )
                        identifier = snapshot.call.exchange_id
                        identify_exchange(identifier)
                        directory = self._state / identifier
                        directory.mkdir(mode=0o700, exist_ok=True)
                        _save(
                            directory / "job.json",
                            _encode_json(
                                {
                                    "deadline": snapshot.call.deadline,
                                    "request": key.model_dump(),
                                }
                            ),
                        )
                        deadline = (
                            time.monotonic() + snapshot.call.deadline - time.time()
                        )
                        attempted = False
                        raw_response = None
                        preparation.close()
                        if snapshot.call.phase in ("reserved", "assigned"):
                            preparation.enter_context(self._preparation)
                        continue
                    if not attempted and snapshot.call.phase != "failed":
                        if snapshot.call.phase in ("reserved", "assigned"):
                            openai_request.load_credentials(
                                self._config, self._mode, self._api_key
                            )
                            start_id = self._gateway.ensure_pod()
                            snapshot = self._gateway.dispatch(key, start_id)
                        if snapshot.session is not None:
                            session = cast(Session, snapshot.session)
                            host, path = openai_request.PROVIDERS[self._mode]
                            if any(
                                session.get(k) != v
                                for k, v in {
                                    "id": identifier,
                                    "protocol": 2,
                                    "provider": f"https://{host}{path}",
                                    "proof_key_sha256": self._prover.verifying_key_sha256,
                                    "miner": self._hotkey.ss58_address,
                                    "round": self._config["round"],
                                }.items()
                            ):
                                raise WitnessError(
                                    "Witness session differs",
                                    identifier,
                                    code="invalid_recording",
                                )
                            attempted = True
                            _save(directory / "session.json", _encode_json(session))
                            request = openai_request.prepare_request(
                                self._config,
                                self._mode,
                                self._api_key,
                                body=body,
                                session_id=session_id,
                            )
                            _save(directory / "request.json", body)
                            try:
                                with self._gateway.tunnel(identifier) as tunnel:

                                    def authorize(disclosure: EarlyDisclosure) -> None:
                                        self._authorize(tunnel, disclosure)
                                        preparation.close()

                                    raw_response = exchange(
                                        tunnel,
                                        request.host,
                                        request.chunks,
                                        handshake=lambda disclosure: self._handshake(
                                            tunnel, disclosure
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
                                snapshot = self._gateway.snapshot(identifier)
                                if snapshot.call.phase == "assigned":
                                    # An unclaimed exchange can reconnect when admission becomes available.
                                    attempted = False
                    if time.monotonic() >= deadline:
                        raise ExchangePending(
                            "Capture remains unresolved; reuse the same request_id and body",
                            identifier,
                        )
                    snapshot = self._gateway.snapshot(identifier)
                    if snapshot.call.phase != "issued":
                        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            if snapshot.call.phase == "failed":
                raise WitnessError(
                    "Recorded witness capture failed",
                    identifier,
                    code="capture_failed",
                    source="gateway",
                )
            session, status, response = self._gateway.recorded(
                key,
                snapshot.call,
                self._prover.verifying_key_sha256,
                request=body,
                raw_response=raw_response,
            )
            captured = CapturedResponse(identifier, status, response)
            self._save_response(captured)
            return captured
        except WitnessError as error:
            error.exchange_id = error.exchange_id or identifier
            raise
        except Exception as error:
            raise WitnessError(
                "Witness capture did not complete",
                identifier,
                code="capture_unresolved",
                source="gateway",
            ) from error


def _encode_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _disclosure_claim(disclosure: HandshakeDisclosure) -> HandshakeClaim:
    compression = zlib.compressobj(wbits=-15)
    encoded = (
        compression.compress(_encode_json(disclosure["disclosures"]))
        + compression.flush()
    )
    return {
        "disclosures": base64.b64encode(encoded).decode(),
        "response_key": disclosure["response_key"],
        "client_bytes": disclosure["client_bytes"],
        "probe": True,
    }

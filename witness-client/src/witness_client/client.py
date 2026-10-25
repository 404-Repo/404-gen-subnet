"""Send OpenAI requests through the subnet witness."""

from __future__ import annotations

import base64
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
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Self, Unpack, cast
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
from .calls import Generation, Invocation, RequestKey
from .errors import ExchangePending, WitnessError
from .pods import PodGatewayClient
from .proof import Prover
from .response import completed_response, provider_error
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
    Usage,
    WitnessConfig,
)

if TYPE_CHECKING:
    from ._tls import TLSAbruptCloseError
else:
    from tlslite.errors import TLSAbruptCloseError


@dataclass(frozen=True)
class WitnessResult:
    """Completed exchange with the full OpenAI response in ``response``.

    ``output_text`` and ``usage`` give convenient access to text and token
    counts. ``response.output`` also preserves tool calls, refusals, and reasoning
    items. The gateway assigns exchanges to the active competition round.
    """

    hotkey: str
    output_text: str
    usage: Usage
    response: Response = field(kw_only=True)
    _exchange_id: str = field(repr=False, kw_only=True)


class WitnessClient:
    """Send prompts to OpenAI through the subnet's witness server.

    Bind a generation with for_generation() or use run_generation(). The
    generation client returns the recorded OpenAI result so the validator can
    replay the miner's code without calling OpenAI again.
    """

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
                across generations (1 through 32, default 3).
        """
        self._config: WitnessConfig
        if isinstance(config, dict):
            self._config = config.copy()
        else:
            self._config = json.loads(Path(config).read_text(encoding="utf-8"))
        if self._config.get("transport", "pods-v1") != "pods-v1":
            raise ValueError("Only witness Pods are supported; use a Pod gateway and omit 'transport'")
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self._config.get("round", "")) is None:
            raise ValueError("Pod transport requires a pinned competition round")
        if (
            not isinstance(parallel_preparations, int)
            or isinstance(parallel_preparations, bool)
            or not 1 <= parallel_preparations <= 32
        ):
            raise ValueError("parallel_preparations must be an integer from 1 through 32")
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

    @timed("create")
    def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[True] = True,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> WitnessResult:
        """Require a generation-bound client before making a witnessed request."""
        raise ValueError("Use for_generation() or run_generation() with the Pod transport")

    def release(self, exchange_id: str) -> None:
        """Release a stuck capture without resetting the generation's usage.

        This is safe to repeat.
        An OpenAI request already in progress may still finish and save evidence;
        use resume() to retrieve it. Releasing does not issue another GPT request.
        """
        self._gateway.release(str(uuid.UUID(exchange_id)))

    def renderer_transport(self) -> httpx.BaseTransport:
        """Authenticate RendererClient requests to this miner's assigned Pod."""
        return httpx.MockTransport(self._gateway.render)

    def for_generation(
        self,
        image: bytes,
        seed: int,
        *,
        prompt_url: str | None = None,
        call_slots: threading.Semaphore | None = None,
    ) -> GenerationWitness:
        """Bind the supplied generate() client to one prompt and generation seed.

        The trusted runner constructs this client outside miner code. Repeating
        a generation resumes its existing prefix; it does not reset prompt usage.
        Shared call slots bound active captures across generations; reservation
        and recorded delivery waits do not occupy a slot.
        """
        return GenerationWitness(
            self,
            Generation.from_prompt(
                image,
                seed,
                round_number=self._config["round"],
                prompt_url=prompt_url,
            ),
            call_slots=call_slots,
        )

    @timed("resume")
    def resume(self, exchange_id: str) -> WitnessResult:
        """Retrieve a recorded result without making another OpenAI request.

        Pass the exchange_id from WitnessError and reuse the original
        state_directory, hotkey, and authentication mode during the active
        generation phase. Local recovery files and retained witness evidence
        must still be available. Returns
        the same WitnessResult format as create(); raises WitnessError if the
        exchange is unavailable, unfinished, or rejected by the provider.
        """
        identifier = str(uuid.UUID(exchange_id))
        identify_exchange(identifier)
        job = json.loads((self._state / identifier / "job.json").read_bytes())
        invocation = Invocation.model_validate(job["invocation"])
        if invocation.request.provider != self._mode:
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
                session, recorded_status, response = self._gateway.recorded(
                    invocation, snapshot.call, self._prover.verifying_key_sha256, request=request,
                )
                return recorded_result(
                    session["miner"], identifier, recorded_status, response
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


class GenerationWitness(WitnessClient):
    """One generate() invocation; equal requests share a captured response."""

    def __init__(
        self,
        parent: WitnessClient,
        generation: Generation,
        *,
        call_slots: threading.Semaphore | None = None,
    ) -> None:
        self._config, self._mode, self._api_key = (
            parent._config,
            parent._mode,
            parent._api_key,
        )
        self._state, self._prover, self._hotkey = (
            parent._state,
            parent._prover,
            parent._hotkey,
        )
        self._preparation = parent._preparation
        self._http, self._gateway = parent._http, parent._gateway
        self.generation = generation
        self.responses = self
        self._cursor = 0
        self._invocations = threading.Condition()
        self._prefix: list[str] | None = None
        self._call_slots = call_slots
        self._deliveries = threading.Condition()
        self._delivery_cursor = 0
        self._unresolved = False

    def close(self) -> None:
        """Keep shared connections open until the parent WitnessClient closes."""

    def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[True] = True,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> WitnessResult:
        openai_request.validate_session_id(session_id, self._mode)
        normalized = cast(
            ResponseCreateParamsStreaming, {"service_tier": "default", **params, "stream": stream}
        )
        body = openai_request.serialize_body(normalized, self._mode)
        key = RequestKey.from_body(self._mode, body, request_id)
        identifier: str | None = None
        retries = 0
        raw_response: bytes | None = None
        try:
            # Serialize reservation only. TLS, inference and result waiting remain
            # concurrent, including multiple invocations of the same request.
            with self._invocations:
                if self._prefix is None:
                    try:
                        recording = self._gateway.recording(self.generation)
                    except WitnessError as error:
                        if error.status_code != 404:
                            raise
                        self._prefix = []
                    else:
                        if recording.generation.id != self.generation.id:
                            raise WitnessError(
                                "Generation recording differs",
                                code="invalid_recording",
                                source="gateway",
                            )
                        self._prefix = [entry.call_id for entry in recording.entries]
                reservation_deadline = time.monotonic() + self._gateway.delivery_timeout
                while (
                    self._cursor < len(self._prefix)
                    and self._prefix[self._cursor] != key.id
                ):
                    remaining = reservation_deadline - time.monotonic()
                    if self._unresolved or remaining <= 0:
                        raise WitnessError(
                            "Recorded invocation order could not be restored",
                            code="recording_unresolved",
                            source="gateway",
                        )
                    self._invocations.wait(remaining)
                invocation = Invocation(
                    generation=self.generation, ordinal=self._cursor, request=key
                )
                snapshot = self._gateway.reserve(invocation)
                if (
                    snapshot.call.phase == "failed"
                    and snapshot.call.not_dispatched
                    and not snapshot.call.authorized
                ):
                    snapshot = self._gateway.retry(invocation, snapshot.call.exchange_id)
                    retries += 1
                identifier = snapshot.call.exchange_id
                identify_exchange(identifier)
                if (
                    snapshot.call.request != key
                    or snapshot.hotkey != self._hotkey.ss58_address
                    or snapshot.round != self.generation.round
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
                            "invocation": invocation.model_dump(),
                        }
                    ),
                )
                self._cursor += 1
                self._invocations.notify_all()
            attempted = False
            deadline = time.monotonic() + snapshot.call.deadline - time.time()
            with (
                self._call_slots
                if self._call_slots is not None
                and invocation.ordinal == snapshot.call.owner_ordinal
                and snapshot.call.phase not in ("issued", "failed")
                else nullcontext(),
                ExitStack() as preparation,
            ):
                # Bound live preparation before opening provider connections.
                # Release its slot after authorization so inference can overlap.
                if (
                    invocation.ordinal == snapshot.call.owner_ordinal
                    and snapshot.call.phase in ("reserved", "assigned")
                ):
                    preparation.enter_context(self._preparation)
                while snapshot.call.phase != "issued":
                    if (
                        snapshot.call.phase == "failed"
                        and snapshot.call.not_dispatched
                        and not snapshot.call.authorized
                    ):
                        if retries >= 2:
                            break
                        retries += 1
                        time.sleep(retries)
                        snapshot = self._gateway.retry(invocation, snapshot.call.exchange_id)
                        if (snapshot.call.request != key or snapshot.hotkey != self._hotkey.ss58_address
                                or snapshot.round != self.generation.round):
                            raise WitnessError("Retry reservation differs", code="invalid_recording")
                        identifier = snapshot.call.exchange_id
                        identify_exchange(identifier)
                        directory = self._state / identifier
                        directory.mkdir(mode=0o700, exist_ok=True)
                        _save(directory / "job.json", _encode_json({
                            "deadline": snapshot.call.deadline, "invocation": invocation.model_dump(),
                        }))
                        deadline = time.monotonic() + snapshot.call.deadline - time.time()
                        attempted = False
                        raw_response = None
                        preparation.close()
                        if (invocation.ordinal == snapshot.call.owner_ordinal
                                and snapshot.call.phase in ("reserved", "assigned")):
                            preparation.enter_context(self._preparation)
                        continue
                    if (
                        invocation.ordinal == snapshot.call.owner_ordinal
                        and not attempted
                        and snapshot.call.phase != "failed"
                    ):
                        if snapshot.call.phase in ("reserved", "assigned"):
                            openai_request.load_credentials(
                                self._config, self._mode, self._api_key
                            )
                            start_id = self._gateway.ensure_pod()
                            snapshot = self._gateway.dispatch(invocation, start_id)
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
                                    "round": snapshot.round,
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
                            "Capture remains unresolved; restart with the same generation", identifier
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
                invocation, snapshot.call, self._prover.verifying_key_sha256,
                request=body, raw_response=raw_response,
            )
            result: WitnessResult | None = None
            failure: WitnessError | None = None
            try:
                result = recorded_result(session["miner"], identifier, status, response)
            except WitnessError as error:
                failure = error
            with self._deliveries:
                delivery = self._gateway.delivered(invocation)
                delivery_deadline = time.monotonic() + self._gateway.delivery_timeout
                while delivery != self._delivery_cursor:
                    remaining = delivery_deadline - time.monotonic()
                    if self._unresolved or remaining <= 0:
                        raise WitnessError(
                            "Recorded delivery order could not be restored",
                            identifier,
                            code="recording_unresolved",
                            source="gateway",
                        )
                    self._deliveries.wait(remaining)
                self._delivery_cursor += 1
                self._deliveries.notify_all()
            if failure is not None:
                raise failure
            assert result is not None
            return result
        except WitnessError as error:
            if error.source != "provider":
                with self._deliveries:
                    self._unresolved = True
                    self._deliveries.notify_all()
                with self._invocations:
                    self._invocations.notify_all()
            error.exchange_id = error.exchange_id or identifier
            raise
        except Exception as error:
            with self._deliveries:
                self._unresolved = True
                self._deliveries.notify_all()
            with self._invocations:
                self._invocations.notify_all()
            raise WitnessError(
                "Generation recording did not complete",
                identifier,
                code="recording_unresolved",
                source="gateway",
            ) from error

    def finish(self, output: str | None) -> None:
        if self._unresolved:
            raise WitnessError(
                "An infrastructure failure left this attempt unresolved",
                code="recording_unresolved",
                source="gateway",
            )
        self._gateway.finish_generation(self.generation, self._cursor, output)


def recorded_result(
    hotkey: str, identifier: str, status: int, body: bytes
) -> WitnessResult:
    """The same immutable provider outcome is decoded in generation and replay."""
    try:
        if status != 200:
            raise provider_error(status, body)
        try:
            output, completed = completed_response(body)
        except ValueError as error:
            raise WitnessError(
                "Invalid recorded OpenAI response",
                identifier,
                source="provider",
                code="invalid_provider_response",
            ) from error
        return WitnessResult(
            hotkey,
            output,
            cast(Usage, completed.usage.model_dump(exclude_unset=True))
            if completed.usage
            else {},
            response=completed,
            _exchange_id=identifier,
        )
    except WitnessError as error:
        error.exchange_id = identifier
        raise


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

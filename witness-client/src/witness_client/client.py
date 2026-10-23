"""Send OpenAI requests through the subnet witness."""

from __future__ import annotations

import base64
import json
import math
import os
import ssl
import time
import uuid
import zlib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, Unpack, cast
from urllib.parse import urlsplit

import httpx
from bittensor_wallet import Keypair
from openai.types.responses import Response
from openai.types.responses.response_create_params import ResponseCreateParamsStreaming

from . import recovery
from . import request as openai_request
from .errors import WitnessError
from .gateway import GatewayClient
from .proof import Prover
from .response import completed_response, http_body, provider_error
from .state import save as _save
from .timing import identify_exchange, measure, timed
from .transport import exchange
from .tunnel import WebSocketTunnel
from .types import (
    Claim,
    EarlyDisclosure,
    HandshakeClaim,
    HandshakeDisclosure,
    Usage,
    WitnessConfig,
)

# Seconds of stream silence between status reads while a response is pending.
_STATUS_INTERVAL = 60.0


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

    Call create() with a model and text or images. It returns generated text
    and token usage. The witness saves the request and response so the
    validator can replay the miner's code without calling OpenAI again.
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
        stall_timeout: float = 600,
    ) -> None:
        """Configure credentials, proof parameters, and local recovery storage.

        Args:
            config: WitnessConfig mapping or path to its JSON file. Requires
                witness_url (HTTPS origin) and proof_parameters path.
                Set state_directory to keep recovery files on persistent
                storage; otherwise the user's local state directory is used.
            hotkey: Miner signing key loaded and managed by the caller.
            openai_api_key: OpenAI API key. Choose this or subscription.
            subscription: Use "codex" for an existing local Codex login.
                Credentials are refreshed when needed.
            delivery_timeout: Positive retry window in seconds for each gateway
                delivery phase (default 600), not a total create() time limit.
            stall_timeout: Positive seconds that create() keeps waiting on a
                silent live stream after a status read reports the exchange
                issued (default 600). It then closes the stream and returns
                the recorded result as resume() does, without another request.
        """
        self._config: WitnessConfig
        if isinstance(config, dict):
            self._config = config.copy()
        else:
            self._config = json.loads(Path(config).read_text(encoding="utf-8"))
        if not math.isfinite(delivery_timeout) or delivery_timeout <= 0:
            raise ValueError("delivery_timeout must be finite and > 0 seconds")
        if not math.isfinite(stall_timeout) or stall_timeout <= 0:
            raise ValueError("stall_timeout must be finite and > 0 seconds")
        self._stall_timeout = stall_timeout
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
        self._gateway = GatewayClient(
            self._http,
            self._hotkey,
            self._state,
            delivery_timeout,
        )
        # Preserve the familiar Responses API spelling without an extra layer.
        self.responses = self

    def close(self) -> None:
        """Close HTTP connections, keeping saved state available for recovery."""
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
    def create(self, **params: Unpack[ResponseCreateParamsStreaming]) -> WitnessResult:
        """Make one OpenAI request and wait for its witnessed response.

        Args:
            **params: OpenAI Responses request parameters, typed by the SDK's
                ResponseCreateParamsStreaming. Supply stream=True, model, and
                input directly, e.g. model="YOUR_MODEL_ID",
                input="Make a three.js scene", instructions="Return JavaScript",
                reasoning={"effort": "high"}, or prompt_cache_key="lorry".
                Support depends on the model and API/subscription endpoint.
                This method returns the completed result after consuming the
                stream. Background generation is unsupported. Subscription
                requests always use store=False.

        Returns:
            WitnessResult with the full OpenAI response, hotkey, output_text,
            and usage. Tool-call and refusal results can have empty output_text.

        Raises:
            ValueError: Invalid local request options, credentials, or body size.
            WitnessError: Worker startup or exchange failed. If exchange_id is set,
                keep it for resume(); inference is never automatically repeated.
                Incomplete/failed generations retain their data in error.response.

        Example:
            from bittensor_wallet import Wallet

            hotkey = Wallet(name="miner", hotkey="default").hotkey
            image_url = "https://example.org/lorry.png"
            with WitnessClient("witness.json", hotkey=hotkey, subscription="codex") as client:
                result = client.create(
                    stream=True,
                    model="YOUR_MODEL_ID",
                    input=[{"role": "user", "content": [
                        {"type": "input_text", "text": "Make this in three.js"},
                        {"type": "input_image", "image_url": image_url, "detail": "auto"},
                    ]}],
                    instructions="Return only JavaScript.",
                )
                print(result.output_text, result.usage)
        """
        with measure("serialize_request"):
            body = openai_request.serialize_body(params, self._mode)
        with measure("credentials_before_queue"):
            openai_request.load_credentials(self._config, self._mode, self._api_key)
        host, path = openai_request.PROVIDERS[self._mode]
        identifier: str | None = None
        try:
            session = self._gateway.queue_and_wait_for_worker(self._mode)
            identifier = str(uuid.UUID(session["id"]))
            if (
                session.get("protocol") != 2
                or session["provider"] != f"https://{host}{path}"
                or session["proof_key_sha256"] != self._prover.verifying_key_sha256
                or session["miner"] != self._hotkey.ss58_address
            ):
                raise WitnessError(
                    "Witness session differs from the requested exchange", identifier
                )
            directory = self._state / identifier
            fd = os.open(self._state, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            _save(directory / "session.json", _encode_json(session))
            try:
                with measure("prepare_request_and_credentials"):
                    request = openai_request.prepare_request(
                        self._config,
                        self._mode,
                        self._api_key,
                        body=body,
                    )
            except Exception:
                # Release the unused worker without masking the credential error.
                with suppress(Exception):
                    self._gateway.release(identifier)
                raise
            _save(directory / "request.json", request.body)
            try:
                with self._gateway.tunnel(identifier) as tunnel:
                    raw = exchange(
                        tunnel,
                        request.host,
                        request.chunks,
                        handshake=lambda disclosure: self._handshake(
                            tunnel, disclosure
                        ),
                        authorize=lambda disclosure: self._authorize(
                            tunnel, disclosure
                        ),
                        timeout=self._gateway.remaining_seconds(identifier),
                    )
            except _StalledDelivery:
                # The tunnel is closed; the issued evidence holds the response.
                return self.resume(identifier)
            with measure("parse_and_save_response"):
                response = http_body(raw)
                _save(directory / "response.bin", response)
            return self.resume(identifier)
        except WitnessError as error:
            error.exchange_id = error.exchange_id or identifier
            raise
        except Exception as error:
            raise WitnessError(
                f"Exchange {identifier} failed; inference was not retried: {error}",
                identifier,
            ) from error

    def release(self, exchange_id: str) -> None:
        """Release a stuck allocation so the next create() can queue a new worker.

        This is safe to repeat and does not reset the round's worker allowance.
        An OpenAI request already in progress may still finish and save evidence;
        use resume() to retrieve it. Releasing does not issue another GPT request.
        """
        self._gateway.release(str(uuid.UUID(exchange_id)))

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
        try:
            directory = self._state / identifier
            session = recovery.load_session(directory)
            host, path = openai_request.PROVIDERS[self._mode]
            if session["provider"] != f"https://{host}{path}":
                raise WitnessError(
                    "Cannot resume an exchange from a different provider",
                    identifier,
                )
            with measure("evidence_retrieval_wait"):
                status = self._gateway.wait_for_evidence(identifier)
            if status["status"] != "issued":
                raise WitnessError("Witness has not accepted this exchange", identifier)
            provider_status = status.get("provider_status")
            if not (status.get("evidence_expires") or 0) > time.time() * 1000:
                raise WitnessError(
                    "Witness evidence is unavailable; use a server with evidence retention",
                    identifier,
                )
            if not (directory / "response.bin").exists():
                _save(directory / "response.bin", self._gateway.response(identifier))
            if session["miner"] != self._hotkey.ss58_address:
                raise WitnessError(
                    "Cannot resume an exchange from a different miner", identifier
                )
            body = (directory / "response.bin").read_bytes()
            if provider_status is not None and provider_status != 200:
                raise provider_error(provider_status, body)
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
                session["miner"],
                output,
                (
                    cast(Usage, completed.usage.model_dump(exclude_unset=True))
                    if completed.usage
                    else {}
                ),
                response=completed,
                _exchange_id=identifier,
            )
        except WitnessError as error:
            error.exchange_id = identifier
            raise
        except Exception as error:
            raise WitnessError(
                f"Could not recover the recorded exchange: {error}",
                identifier,
                code="recovery_unavailable",
            ) from error

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
        # From now on the worker can issue evidence before the stream delivers it.
        tunnel.watch(
            _DeliveryWatch(self._gateway, tunnel.identifier, self._stall_timeout),
            min(_STATUS_INTERVAL, self._stall_timeout),
        )


class _StalledDelivery(WitnessError):
    """The witness issued an exchange that its live stream did not deliver."""


class _DeliveryWatch:
    """Detect an issued exchange whose live stream stays silent.

    The witness saves evidence before releasing response bytes, so an issued
    exchange can be retrieved by resume() without another provider request.
    """

    def __init__(self, gateway: GatewayClient, identifier: str, timeout: float) -> None:
        self.gateway, self.identifier, self.timeout = gateway, identifier, timeout
        self.issued: float | None = None

    def __call__(self) -> None:
        if self.issued is None:
            try:
                with measure("delivery_status_poll"):
                    status = self.gateway.status(self.identifier)
            except (httpx.HTTPError, WitnessError):
                return  # A failed status read never ends a stream that may deliver.
            if status["status"] != "issued":
                return
            self.issued = time.monotonic()
        if time.monotonic() - self.issued >= self.timeout:
            raise _StalledDelivery(
                "Witness issued the exchange but its live stream did not deliver it",
                self.identifier,
                code="delivery_stalled",
                source="gateway",
            )


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

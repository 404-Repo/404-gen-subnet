"""SDK configuration, canonical requests and captured-response values."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx
from bittensor_wallet import Keypair
from openai.types.responses.response_create_params import (
    ResponseCreateParamsBase,
    ResponseCreateParamsStreaming,
)

from . import request as openai_request
from .calls import RequestKey
from .delivery import _raise_response_error
from .errors import WitnessError
from .types import (
    HandshakeClaim,
    HandshakeDisclosure,
    WitnessConfig,
)


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


class _ClientState:
    """Configuration and canonical requests shared by live and recorded clients."""

    def __init__(
        self,
        config: WitnessConfig | str | os.PathLike[str],
        *,
        hotkey: Keypair,
        openai_api_key: str | None,
        subscription: Literal["codex"] | None,
        delivery_timeout: float,
        parallel_calls: int,
        parallel_preparations: int,
    ) -> None:
        self._config: WitnessConfig
        if isinstance(config, dict):
            self._config = config.copy()
        else:
            self._config = json.loads(Path(config).read_text(encoding="utf-8"))
        if (
            not isinstance(parallel_calls, int)
            or isinstance(parallel_calls, bool)
            or not 1 <= parallel_calls <= 32
        ):
            raise ValueError("parallel_calls must be an integer from 1 through 32")
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
        self._parameters = Path(self._config["proof_parameters"]).expanduser().resolve()

        self._hotkey = hotkey

    def _request(
        self, request_id: int, session_id: str | None, params: ResponseCreateParamsBase
    ) -> tuple[RequestKey, bytes]:
        openai_request.validate_session_id(session_id, self._mode)
        normalized = cast(
            ResponseCreateParamsStreaming,
            {"service_tier": "default", **params, "stream": True},
        )
        body = openai_request.serialize_body(normalized, self._mode)
        return (
            RequestKey.from_body(self._mode, body, request_id, session_id=session_id),
            body,
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


def _submission_body(image: bytes, source: str | None) -> dict[str, object]:
    if not isinstance(image, bytes):
        raise ValueError("image must contain the frozen prompt bytes")
    if len(image) > 8 * 1024 * 1024:
        raise ValueError("Prompt image exceeds 8 MiB")
    if source is not None:
        if not isinstance(source, str):
            raise ValueError("generate() must return JavaScript or None")
        if len(source.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Source exceeds the renderer's 1 MiB limit")
    return {
        "generation": {"prompt_sha256": hashlib.sha256(image).hexdigest()},
        "source": source,
        "prompt_image": (
            base64.b64encode(image).decode("ascii") if source is not None else None
        ),
    }


def _submission_result(response: httpx.Response) -> dict[str, object]:
    if len(response.content) > 256 * 1024:
        raise WitnessError(
            "Oversized final submission response",
            code="invalid_submission",
            source="gateway",
        )
    if not response.is_success:
        _raise_response_error(response, response.content)
    try:
        value = response.json()["submission"]
        if not isinstance(value, dict):
            raise ValueError("Expected a saved submission")
        return cast(dict[str, object], value)
    except (ValueError, KeyError, TypeError) as error:
        raise WitnessError(
            "Invalid final submission response",
            code="invalid_submission",
            source="gateway",
        ) from error


def _submissions_result(response: httpx.Response) -> dict[str, dict[str, object]]:
    # The saved index is bounded to 16 MiB; the JSON envelope adds 16 bytes.
    if len(response.content) > 16 * 1024 * 1024 + 16:
        raise WitnessError(
            "Oversized submissions response",
            code="invalid_submissions",
            source="gateway",
        )
    if not response.is_success:
        _raise_response_error(response, response.content)
    try:
        value = response.json()["submissions"]
        if not isinstance(value, dict) or any(
            not isinstance(prompt, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", prompt) is None
            or not isinstance(result, dict)
            for prompt, result in value.items()
        ):
            raise ValueError("Expected saved submissions keyed by official prompt ID")
        return cast(dict[str, dict[str, object]], value)
    except (ValueError, KeyError, TypeError) as error:
        raise WitnessError(
            "Invalid submissions response", code="invalid_submissions", source="gateway"
        ) from error

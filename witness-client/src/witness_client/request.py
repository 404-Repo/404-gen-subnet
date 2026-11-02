"""Prepare one OpenAI request, keeping its credential in a separate TLS chunk."""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from openai.types.responses.response_create_params import ResponseCreateParamsStreaming
from pydantic import BaseModel

from .auth import subscription_credentials
from .errors import WitnessError
from .types import WitnessConfig

PROVIDERS = {
    "api": ("api.openai.com", "/v1/responses"),
    "codex": ("chatgpt.com", "/backend-api/codex/responses"),
}


@dataclass(frozen=True)
class OpenAIRequest:
    host: str
    path: str
    body: bytes
    chunks: tuple[tuple[bool, bytes], ...] = field(repr=False)

    @property
    def url(self) -> str:
        return f"https://{self.host}{self.path}"


def serialize_body(
    params: ResponseCreateParamsStreaming,
    mode: Literal["api", "codex"],
) -> bytes:
    """Serialize OpenAI parameters with the witness's request settings."""
    if params.get("stream") is not True:
        raise ValueError(
            "Witness transport requires streaming; result is returned after completion"
        )
    background = params.get("background")
    if background is not None and background is not False:
        raise ValueError("Witness requests do not support background generation")
    if "input" in params and not isinstance(params["input"], (str, list)):
        raise ValueError(
            "OpenAI input must be a prompt string or a list of input items"
        )
    request: dict[str, object] = {
        "tools": [],
        **params,
    }
    if mode == "codex":
        request["store"] = False
        request.setdefault("instructions", "")
        prompt = request.get("input")
        if isinstance(prompt, str):
            request["input"] = [
                {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
            ]
    body = json.dumps(
        request,
        default=_json_default,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if len(body) > 8 * 1024 * 1024:
        raise ValueError("Request body exceeds 8 MiB")
    return body


def load_credentials(
    config: WitnessConfig,
    mode: Literal["api", "codex"],
    api_key: str | None,
) -> tuple[str, str | None]:
    """Load and validate credentials, refreshing a subscription when needed."""
    credential: str | None
    account: str | None
    if mode == "codex":
        try:
            credential, account = subscription_credentials(
                config.get("codex_home"), config.get("codex", "codex")
            )
        except (OSError, RuntimeError) as error:
            raise WitnessError(
                f"Could not load ChatGPT subscription credentials: {error}",
                code="credentials_unavailable",
            ) from error
    else:
        credential, account = api_key, None
    if not isinstance(credential, str) or not re.fullmatch(
        r"[A-Za-z0-9._~+/=-]{64,4096}", credential
    ):
        raise ValueError("Unsupported OpenAI credential format")
    if account is not None and not re.fullmatch(r"[a-f0-9-]{1,80}", account):
        raise ValueError("Invalid subscription account identifier")
    return credential, account


def prepare_request(
    config: WitnessConfig,
    mode: Literal["api", "codex"],
    api_key: str | None,
    *,
    body: bytes,
    session_id: str | None = None,
) -> OpenAIRequest:
    """Build HTTP chunks for a serialized body using current credentials."""
    validate_session_id(session_id, mode)
    credential, account = load_credentials(config, mode, api_key)
    host, path = PROVIDERS[mode]
    prefix = (
        f"POST {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
        f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n"
        "Accept: text/event-stream\r\nAuthorization: Bearer "
    ).encode()
    suffix = "\r\n"
    if account:
        suffix += f"ChatGPT-Account-Id: {account}\r\n"
    if session_id is not None:
        suffix += f"session-id: {session_id}\r\n"
    suffix_bytes = (suffix + "\r\n").encode() + body
    return OpenAIRequest(
        host=host,
        path=path,
        body=body,
        chunks=((False, prefix), (True, credential.encode()), (False, suffix_bytes)),
    )


def validate_session_id(value: str | None, mode: Literal["api", "codex"]) -> None:
    """Keep optional subscription routing metadata within the 8 KiB header limit."""
    if value is None:
        return
    if mode != "codex":
        raise ValueError("session_id requires subscription='codex'")
    if not isinstance(value, str) or re.fullmatch(r"[!-~]{1,1024}", value) is None:
        raise ValueError("session_id must contain 1-1024 visible ASCII characters")


def _json_default(value: object) -> object:
    """Convert additional request types for the standard JSON encoder."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_unset=True)
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, Iterable) and not isinstance(
        value, (bytes, bytearray, memoryview)
    ):
        return list(value)
    raise ValueError(f"Unsupported OpenAI request value: {type(value).__name__}")

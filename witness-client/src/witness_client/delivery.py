"""Bounded HTTP and one retry policy for witness delivery, never inference."""

import hashlib
import json
import random
import ssl
import time
import uuid
from collections.abc import Callable
from typing import Literal, TypeAlias, TypeVar
from urllib.parse import urlsplit

import httpx

from .errors import WitnessError

Phase: TypeAlias = Literal["handshake", "authorize"]
T = TypeVar("T")


def request(
    http: httpx.Client,
    method: str,
    path: str,
    body: bytes | None = None,
    *,
    headers: dict[str, str],
    limit: int = 32768,
    extensions: dict[str, object] | None = None,
) -> bytes:
    """Perform exactly one bounded request; callers own its retry deadline."""
    with http.stream(method, path, content=body, headers=headers, extensions=extensions) as response:
        result = bytearray()
        bound = limit if response.is_success else 32768
        for chunk in response.iter_bytes():
            result.extend(chunk)
            if len(result) > bound:
                raise WitnessError("Oversized witness response", source="gateway")
        if not response.is_success:
            code = "request_failed"
            identifier: str | None = None
            try:
                value = json.loads(result)
                if isinstance(value, dict) and isinstance(value.get("error"), str):
                    code = value["error"]
                    if code == "active_request_exists" and isinstance(
                        value.get("exchange_id"), str
                    ):
                        identifier = str(uuid.UUID(value["exchange_id"]))
            except ValueError:
                pass  # Never expose a proxy's arbitrary HTML/error body.
            raise WitnessError(
                f"Witness {response.status_code}: {code}",
                identifier,
                status_code=response.status_code,
                code=code,
                source="gateway",
            )
        return bytes(result)


def retry(
    operation: Callable[[], T],
    *,
    deadline: float,
) -> T:
    """Try once even with a zero window, then retry transient delivery failures."""
    attempt = 0
    while True:
        try:
            return operation()
        except (httpx.TransportError, WitnessError) as error:
            wait_for_retry(error, deadline=deadline, attempt=attempt)
            attempt += 1


def wait_for_retry(
    error: httpx.TransportError | WitnessError, *, deadline: float, attempt: int
) -> None:
    """Wait before retrying, or raise the same error if no retry is allowed.

    ``deadline`` uses time.monotonic(); ``attempt`` is zero for the first failed
    attempt. Transient outages use jittered backoff.
    """
    transient = isinstance(error, httpx.TransportError)
    if isinstance(error, WitnessError):
        transient = error.status_code in (429, 502, 503, 504) and error.code not in (
            "claim_attempts_exhausted",
            "job_limit_reached",
        )
    remaining = deadline - time.monotonic()
    if not transient or remaining <= 0:
        raise error
    delay = random.uniform(1, min(10, 2 ** min(attempt + 1, 4)))
    time.sleep(min(remaining, delay))


class ArtifactClient:
    """Own unsigned evidence downloads and verify their trusted descriptors."""

    def __init__(self) -> None:
        self.http = httpx.Client(
            verify=ssl.create_default_context(), timeout=30, follow_redirects=False, trust_env=False,
        )

    def close(self) -> None:
        self.http.close()

    def download(self, link: object, limit: int, *, delivery_timeout: float) -> bytes:
        if not isinstance(link, dict):
            raise WitnessError("Missing artifact descriptor", code="invalid_recording", source="gateway")
        url = urlsplit(str(link.get("url", "")))
        size = link.get("size")
        if (url.scheme != "https" or not url.hostname or url.username or url.fragment
                or type(size) is not int or not 0 <= size <= limit):
            raise WitnessError("Invalid artifact descriptor", code="invalid_recording", source="gateway")
        raw = retry(lambda: request(self.http, "GET", str(link["url"]), headers={}, limit=limit),
                    deadline=time.monotonic() + delivery_timeout)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != link.get("sha256"):
            raise WitnessError("Downloaded evidence hash differs", code="invalid_recording", source="gateway")
        return raw

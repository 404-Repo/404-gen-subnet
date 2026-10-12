"""Bounded HTTP and one retry policy for witness delivery, never inference."""

import json
import random
import time
import uuid
from collections.abc import Callable
from typing import Literal, TypeAlias, TypeVar

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
) -> bytes:
    """Perform exactly one bounded request; callers own its retry deadline."""
    with http.stream(method, path, content=body, headers=headers) as response:
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

"""Bounded search over infrastructure failures for renders with no saved outcome."""

from __future__ import annotations

import heapq
import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

import httpx
from renderer_client import RenderOptions, RenderUnavailable
from renderer_client.client import _render_response


MAX_RECOVERY_ATTEMPTS = 32
MAX_MISSING_RENDER_RETRIES = 16
MAX_MISSING_RENDER_CALLS = 64
MAX_PENDING_PLANS = 256


class MissingRenderOutcome(RenderUnavailable):
    def __init__(self, request_key: str) -> None:
        self.request_key = request_key
        super().__init__("No saved render for this request after checking known Witness images: " + request_key)


class RenderFailure(StrEnum):
    CONNECTION = "connection"
    HTTP_500 = "http_500"
    TIMEOUT = "timeout"
    HTTP_503 = "http_503"
    HTTP_429 = "http_429"
    HTTP_502 = "http_502"
    HTTP_504 = "http_504"


PREFERRED_FAILURES = (
    RenderFailure.HTTP_500,
    RenderFailure.TIMEOUT,
    RenderFailure.HTTP_503,
    RenderFailure.HTTP_429,
    RenderFailure.HTTP_502,
    RenderFailure.HTTP_504,
    RenderFailure.CONNECTION,
)
FailurePlan = dict[str, tuple[RenderFailure, ...]]


@dataclass(frozen=True)
class MissingRenderAttempt:
    request_key: str
    occurrence: int
    failure: RenderFailure


def assumed_render_failure(failure: RenderFailure, message: str, source: str, options: RenderOptions) -> NoReturn:
    request = httpx.Request(
        "POST", "https://witness.internal/v1/render/grid", params=options.query(), json={"source": source}
    )
    try:
        if failure == RenderFailure.CONNECTION:
            raise httpx.ConnectError(message, request=request)
        if failure == RenderFailure.TIMEOUT:
            raise httpx.ReadTimeout(message, request=request)
        status = int(failure.removeprefix("http_"))
        response = httpx.Response(
            status,
            request=request,
            headers={"Content-Type": "application/json", "Retry-After": "1"},
            content=json.dumps({"error": "Renderer service unavailable"}).encode(),
        )
        _render_response(response)
    except httpx.HTTPError as error:
        raise RenderUnavailable(f"Renderer request failed: {error}") from error
    raise AssertionError("An assumed render failure returned a successful response")


class RenderRecoverySearch:
    """Try simpler error assignments first, including mixed outcomes for retries."""

    def __init__(self, max_attempts: int = MAX_RECOVERY_ATTEMPTS) -> None:
        if max_attempts < 1:
            raise ValueError("Recovery attempts must be at least 1")
        self.max_attempts = max_attempts
        self.attempts = 0
        self._serial = 0
        self._pending: list[tuple[int, int, FailurePlan]] = [(0, 0, {})]
        self._seen: set[str] = {"{}"}

    @staticmethod
    def _identity(plan: FailurePlan) -> str:
        return json.dumps(plan, sort_keys=True)

    def next_plan(self) -> FailurePlan | None:
        if self.attempts >= self.max_attempts or not self._pending:
            return None
        self.attempts += 1
        return heapq.heappop(self._pending)[2]

    def _schedule(self, plan: FailurePlan) -> None:
        normalized = {}
        for key, sequence in plan.items():
            while len(sequence) > 1 and sequence[-1] == sequence[-2]:
                sequence = sequence[:-1]
            if sequence != (RenderFailure.CONNECTION,):
                normalized[key] = sequence
        identity = self._identity(normalized)
        if identity in self._seen:
            return
        self._seen.add(identity)
        self._serial += 1
        heapq.heappush(self._pending, (sum(map(len, normalized.values())), self._serial, normalized))
        if len(self._pending) > MAX_PENDING_PLANS:
            worst = max(range(len(self._pending)), key=lambda index: self._pending[index][:2])
            dropped = self._pending.pop(worst)
            self._seen.remove(self._identity(dropped[2]))
            heapq.heapify(self._pending)

    def observe(self, plan: FailurePlan, attempts: Iterable[MissingRenderAttempt]) -> None:
        histories: dict[str, list[RenderFailure]] = {}
        for attempt in attempts:
            histories.setdefault(attempt.request_key, []).append(attempt.failure)
        for key in reversed(histories):
            for failure in PREFERRED_FAILURES:
                self._schedule({**plan, key: (failure,)})
        for key, history in reversed(histories.items()):
            for occurrence in range(1, len(history)):
                for failure in PREFERRED_FAILURES:
                    if failure != history[occurrence]:
                        self._schedule({**plan, key: (*history[:occurrence], failure)})

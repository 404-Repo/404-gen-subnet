"""Failures preserve the internal exchange handle for recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from openai.types.responses import Response


class WitnessError(RuntimeError):
    """An exchange failed; inference is never automatically repeated.

    ``source`` identifies the client, gateway, or provider. ``status_code`` is
    that service's HTTP status, when available; ``code`` identifies the failure.
    Provider failures can also include OpenAI's message, parameter, and response.
    """

    def __init__(
        self,
        message: str,
        exchange_id: str | None = None,
        *,
        status_code: int | None = None,
        code: str | None = None,
        source: Literal["client", "gateway", "provider"] = "client",
        provider_code: str | None = None,
        param: str | None = None,
        provider_message: str | None = None,
        response: Response | None = None,
    ) -> None:
        self.exchange_id = exchange_id
        self.status_code = status_code
        self.code = code
        self.source = source
        self.provider_code = provider_code
        self.param = param
        self.provider_message = provider_message
        self.response = response
        super().__init__(message)


class ExchangePending(WitnessError):
    """The saved exchange is unresolved; resume it without starting inference."""

    def __init__(
        self,
        message: str,
        exchange: str,
        *,
        code: str = "capture_in_progress",
    ) -> None:
        super().__init__(message, exchange, code=code, source="gateway")

    @property
    def exchange(self) -> str:
        assert self.exchange_id is not None
        return self.exchange_id

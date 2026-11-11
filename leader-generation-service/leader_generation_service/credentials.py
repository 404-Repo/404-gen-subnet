import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from witness_client.auth import _refresh_codex, access_token_expiry, subscription_credentials

from .runtime import execute


ACCESS_TOKEN_MARGIN_SECONDS = 600
CODEX_CLI_VERSION = "0.167.0"


@dataclass(frozen=True)
class AccessCredentials:
    access_token: str = field(repr=False)
    account_id: str

    @property
    def expires_at(self) -> float:
        return access_token_expiry(self.access_token)


class CredentialManager:
    def __init__(
        self,
        home: Path,
        executable: str,
        *,
        load: Callable[[int], AccessCredentials] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.home, self.executable = home, executable
        self.load = load or self._load
        self.clock = clock
        self._current: AccessCredentials | None = None
        self._lock = asyncio.Lock()
        self._checked_cli = False

    def _load(self, min_validity_seconds: int) -> AccessCredentials:
        if not self._checked_cli:
            try:
                version = execute([self.executable, "--version"], timeout=10).strip()
            except Exception as error:
                logger.error("Codex CLI version command failed ({})", type(error).__name__)
                raise
            if version != f"codex-cli {CODEX_CLI_VERSION}".encode():
                logger.error("Codex CLI version mismatch. Expected codex-cli {}.", CODEX_CLI_VERSION)
                raise RuntimeError(f"Leader service requires pinned Codex CLI {CODEX_CLI_VERSION}")
            self._checked_cli = True
        try:
            token, account = subscription_credentials(self.home, self.executable)
            if access_token_expiry(token) < self.clock() + min_validity_seconds:
                _refresh_codex(self.home, self.executable)
                token, account = subscription_credentials(self.home, self.executable)
        except Exception as error:
            logger.error("Codex subscription load or refresh failed ({})", type(error).__name__)
            raise
        return AccessCredentials(token, account)

    async def current(self, *, min_validity_seconds: int = ACCESS_TOKEN_MARGIN_SECONDS) -> AccessCredentials:
        async with self._lock:
            if self._current is None or self._current.expires_at < self.clock() + min_validity_seconds:
                value = await asyncio.to_thread(self.load, min_validity_seconds)
                if value.expires_at < self.clock() + min_validity_seconds:
                    logger.error("Codex subscription refresh returned a token with insufficient validity")
                    raise RuntimeError("Subscription refresh did not provide sufficient validity")
                self._current = value
            return self._current

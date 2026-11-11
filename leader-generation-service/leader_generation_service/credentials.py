import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from witness_client.auth import _refresh_codex, access_token_expiry, subscription_credentials

from .runtime import execute


@dataclass(frozen=True)
class AccessCredentials:
    access_token: str = field(repr=False)
    account_id: str

    @property
    def expires_at(self) -> float:
        return access_token_expiry(self.access_token)

    def encode(self) -> bytes:
        return json.dumps({"access_token": self.access_token, "account_id": self.account_id}).encode()


class CredentialManager:
    def __init__(
        self,
        home: Path,
        executable: str,
        *,
        load: Callable[[], AccessCredentials] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.home, self.executable = home, executable
        self.load = load or self._load
        self.clock = clock
        self._current: AccessCredentials | None = None
        self._lock = asyncio.Lock()
        self._checked_cli = False

    def _load(self) -> AccessCredentials:
        if not self._checked_cli:
            version = execute([self.executable, "--version"], timeout=10).strip()
            if version != b"codex-cli 0.167.0":
                raise RuntimeError("Leader service requires pinned Codex CLI 0.167.0")
            self._checked_cli = True
        token, account = subscription_credentials(self.home, self.executable)
        if access_token_expiry(token) < self.clock() + 600:
            _refresh_codex(self.home, self.executable)
            token, account = subscription_credentials(self.home, self.executable)
        return AccessCredentials(token, account)

    async def current(self) -> AccessCredentials:
        async with self._lock:
            if self._current is None or self._current.expires_at < self.clock() + 600:
                value = await asyncio.to_thread(self.load)
                if value.expires_at < self.clock() + 600:
                    raise RuntimeError("Subscription refresh did not provide sufficient validity")
                self._current = value
            return self._current

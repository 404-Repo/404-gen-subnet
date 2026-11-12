from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger


class DiscordWebhook:
    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send_embed(
        self,
        title: str,
        color: int,
        fields: list[dict[str, Any]] | None = None,
        description: str | None = None,
    ) -> None:
        embed: dict[str, Any] = {
            "title": title,
            "color": color,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if description is not None:
            embed["description"] = description
        if fields:
            embed["fields"] = fields

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(self._webhook_url, json={"embeds": [embed]}, timeout=10.0)
                response.raise_for_status()
        except Exception as error:
            logger.warning("Discord notification failed ({})", type(error).__name__)


class DiscordFailureAlert:
    """Report the first failure of a retry loop until a successful cycle."""

    def __init__(self, webhook: DiscordWebhook | None, *, title: str, action: str) -> None:
        self._webhook = webhook
        self._title = title
        self._action = action
        self._reported = False

    async def notify(self, error: Exception) -> None:
        if self._webhook is None or self._reported:
            return
        self._reported = True
        await self._webhook.send_embed(
            title=self._title,
            color=0xE74C3C,
            description=f"`{type(error).__name__}`\n{self._action}",
        )

    def reset(self) -> None:
        self._reported = False

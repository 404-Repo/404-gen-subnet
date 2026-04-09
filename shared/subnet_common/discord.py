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
        except Exception as e:
            logger.warning(f"Discord notification failed: {e}")

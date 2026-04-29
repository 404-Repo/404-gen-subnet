from subnet_common.discord import DiscordWebhook


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook = DiscordWebhook(webhook_url)

    async def notify_iteration_failed(self, reason: str) -> None:
        await self._webhook.send_embed(
            title="Weights Setter Iteration Failed",
            color=0xE74C3C,
            description=reason,
        )

    async def notify_cycle_error(self, error: Exception) -> None:
        await self._webhook.send_embed(
            title="Weights Setter Error",
            color=0xE74C3C,
            description=f"```{type(error).__name__}: {error}```",
        )


class NullDiscordNotifier(DiscordNotifier):
    def __init__(self) -> None:
        pass

    async def notify_iteration_failed(self, reason: str) -> None:
        pass

    async def notify_cycle_error(self, error: Exception) -> None:
        pass


NULL_DISCORD_NOTIFIER = NullDiscordNotifier()

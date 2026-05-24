from subnet_common.discord import DiscordWebhook


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook = DiscordWebhook(webhook_url)

    async def notify_timeline_change(self, *, round_num: int, body: str) -> None:
        await self._webhook.send_embed(
            title=f"Round {round_num} timeline",
            color=0x3498DB,
            description=f"```\n{body}\n```",
        )

    async def notify_audit_requested(self, *, round_num: int, hotkey: str, defeated: str, margin: float) -> None:
        await self._webhook.send_embed(
            title=f"Round {round_num} Audit Requested",
            color=0x9B59B6,
            description=f"`{hotkey[:10]}` defeated `{defeated[:10]}` by {margin:+.2%}",
        )

    async def notify_round_finalized(self, *, round_num: int, winner: str, reason: str | None = None) -> None:
        desc = f"Winner: `{winner[:10]}`"
        if reason:
            desc += f"\nReason: {reason}"
        await self._webhook.send_embed(
            title=f"Round {round_num} Finalized",
            color=0x2ECC71,
            description=desc,
        )

    async def notify_judge_error(self, *, failed_duels: int, total_duels: int) -> None:
        await self._webhook.send_embed(
            title="Judge LLM Errors",
            color=0xE74C3C,
            description=f"last match: **{failed_duels}** / {total_duels} duels failed",
        )

    async def notify_cycle_error(self, error: Exception) -> None:
        await self._webhook.send_embed(
            title="Judge Service Error",
            color=0xE74C3C,
            description=f"```{type(error).__name__}: {error}```",
        )


class NullDiscordNotifier(DiscordNotifier):
    def __init__(self) -> None:
        pass

    async def notify_timeline_change(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_audit_requested(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_round_finalized(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_judge_error(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_cycle_error(self, error: Exception) -> None:
        pass


NULL_DISCORD_NOTIFIER = NullDiscordNotifier()

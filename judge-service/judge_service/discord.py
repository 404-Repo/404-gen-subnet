import time

from subnet_common.discord import DiscordWebhook


class DiscordNotifier:
    def __init__(self, webhook_url: str, judge_error_cooldown_seconds: int = 600) -> None:
        self._webhook = DiscordWebhook(webhook_url)
        self._judge_error_cooldown = judge_error_cooldown_seconds
        self._last_judge_error_at: float = 0

    async def notify_qualification_complete(self, *, round_num: int, qualified: int, total: int) -> None:
        color = 0x2ECC71 if qualified > 0 else 0xE67E22
        await self._webhook.send_embed(
            title=f"Round {round_num} Qualification Complete",
            color=color,
            description=f"**{qualified}** / {total} miners qualified",
        )

    async def notify_audit_requested(self, *, round_num: int, hotkey: str, critical_prompts: int) -> None:
        await self._webhook.send_embed(
            title=f"Round {round_num} Audit Requested",
            color=0x9B59B6,
            description=f"Miner `{hotkey[:10]}` — {critical_prompts} critical prompts",
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

    async def notify_new_local_leader(self, *, round_num: int, hotkey: str, defeated: str, margin: float) -> None:
        await self._webhook.send_embed(
            title=f"Round {round_num} New Local Leader",
            color=0x3498DB,
            description=f"`{hotkey[:10]}` defeated `{defeated[:10]}` with margin {margin:+.2%}",
        )

    async def notify_timeline_reset(self, *, round_num: int, rejected_hotkeys: set[str]) -> None:
        names = ", ".join(f"`{hk[:10]}`" for hk in sorted(rejected_hotkeys))
        await self._webhook.send_embed(
            title=f"Round {round_num} Timeline Reset",
            color=0xE67E22,
            description=f"Local leaders rejected by audit: {names} — restarting timeline",
        )

    async def notify_judge_error(self, *, failed_duels: int, total_duels: int) -> None:
        now = time.monotonic()
        if now - self._last_judge_error_at < self._judge_error_cooldown:
            return
        self._last_judge_error_at = now
        await self._webhook.send_embed(
            title="Judge LLM Errors",
            color=0xE74C3C,
            description=f"**{failed_duels}** / {total_duels} duels failed in last match",
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

    async def notify_qualification_complete(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_audit_requested(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_round_finalized(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_new_local_leader(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_timeline_reset(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_judge_error(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_cycle_error(self, error: Exception) -> None:
        pass


NULL_DISCORD_NOTIFIER = NullDiscordNotifier()

from abc import ABC, abstractmethod

from subnet_common.competition.match_report import DuelWinner, MatchReport
from subnet_common.discord import DiscordWebhook


class DiscordNotifier(ABC):
    @abstractmethod
    async def notify_timeline_change(self, *, round_num: int, body: str) -> None: ...

    @abstractmethod
    async def notify_audit_requested(self, *, round_num: int, hotkey: str, defeated: str, margin: float) -> None: ...

    @abstractmethod
    async def notify_audit_completed(
        self,
        *,
        round_num: int,
        hotkey: str,
        defender: str,
        repeats: list[tuple[float, float]],
        verified: bool,
    ) -> None: ...

    @abstractmethod
    async def notify_round_finalized(self, *, round_num: int, winner: str, reason: str) -> None: ...

    @abstractmethod
    async def notify_if_judge_error(self, report: MatchReport) -> None: ...

    @abstractmethod
    async def notify_cycle_error(self, error: Exception) -> None: ...


class WebhookDiscordNotifier(DiscordNotifier):
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

    async def notify_audit_completed(
        self,
        *,
        round_num: int,
        hotkey: str,
        defender: str,
        repeats: list[tuple[float, float]],
        verified: bool,
    ) -> None:
        verdict = "VERIFIED" if verified else "REJECTED"
        lines = [
            f"r{i}: submitted {submitted_margin:+.2%} / defender {defender_margin:+.2%}"
            for i, (submitted_margin, defender_margin) in enumerate(repeats, start=1)
        ]
        body = f"`{hotkey[:10]}` vs `{defender[:10]}` — **{verdict}**\n```\n" + "\n".join(lines) + "\n```"
        await self._webhook.send_embed(
            title=f"Round {round_num} Audit Completed",
            color=0x2ECC71 if verified else 0xE67E22,
            description=body,
        )

    async def notify_round_finalized(self, *, round_num: int, winner: str, reason: str) -> None:
        await self._webhook.send_embed(
            title=f"Round {round_num} Finalized",
            color=0x2ECC71,
            description=f"Winner: `{winner[:10]}`\nReason: {reason}",
        )

    async def notify_if_judge_error(self, report: MatchReport) -> None:
        """Notify when any duel's evaluation raised — the multi-stage judge sets
        winner+detail on success and leaves SKIPPED with detail=None when it raised.
        SKIPPED with detail set is a clean preview-missing skip, not a failure."""
        failed_duels = sum(1 for duel in report.duels if duel.winner == DuelWinner.SKIPPED and duel.detail is None)
        if not failed_duels:
            return
        await self._webhook.send_embed(
            title="Judge LLM Errors",
            color=0xE74C3C,
            description=f"last match: **{failed_duels}** / {len(report.duels)} duels failed",
        )

    async def notify_cycle_error(self, error: Exception) -> None:
        await self._webhook.send_embed(
            title="Judge Service Error",
            color=0xE74C3C,
            description=f"```{type(error).__name__}: {error}```",
        )


class NullDiscordNotifier(DiscordNotifier):
    async def notify_timeline_change(self, *, round_num: int, body: str) -> None:
        pass

    async def notify_audit_requested(self, *, round_num: int, hotkey: str, defeated: str, margin: float) -> None:
        pass

    async def notify_audit_completed(
        self,
        *,
        round_num: int,
        hotkey: str,
        defender: str,
        repeats: list[tuple[float, float]],
        verified: bool,
    ) -> None:
        pass

    async def notify_round_finalized(self, *, round_num: int, winner: str, reason: str) -> None:
        pass

    async def notify_if_judge_error(self, report: MatchReport) -> None:
        pass

    async def notify_cycle_error(self, error: Exception) -> None:
        pass


NULL_DISCORD_NOTIFIER = NullDiscordNotifier()

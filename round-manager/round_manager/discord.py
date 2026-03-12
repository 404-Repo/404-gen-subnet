from datetime import datetime

from subnet_common.competition.leader import LeaderEntry
from subnet_common.competition.state import RoundStage
from subnet_common.discord import DiscordWebhook


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook = DiscordWebhook(webhook_url)

    async def notify_round_finalized(
        self,
        completed_round: int,
        next_round: int,
        next_stage: RoundStage,
        next_round_start: datetime,
        leader: LeaderEntry,
    ) -> None:
        await self._webhook.send_embed(
            title=f"Round {completed_round} Finalized",
            color=0x2ECC71,
            fields=[
                {"name": "Next Stage", "value": next_stage.value, "inline": True},
                {"name": "Next Round", "value": str(next_round), "inline": True},
                {"name": "Next Round Start", "value": next_round_start.strftime("%Y-%m-%d %H:%M UTC"), "inline": True},
                {
                    "name": "Current Leader",
                    "value": f"`{leader.hotkey[:8]}...` — `{leader.repo}@{leader.commit[:8]}`\n"
                    f"Weight: {leader.weight:.2f}",
                    "inline": False,
                },
            ],
        )

    async def notify_leader_change(
        self,
        old_leader: LeaderEntry,
        new_leader: LeaderEntry,
        round_num: int,
    ) -> None:
        await self._webhook.send_embed(
            title=f"Leader Changed — Round {round_num}",
            color=0xE67E22,
            fields=[
                {
                    "name": "Previous Leader",
                    "value": f"`{old_leader.hotkey[:8]}...` — `{old_leader.repo}@{old_leader.commit[:8]}`",
                    "inline": False,
                },
                {
                    "name": "New Leader",
                    "value": f"`{new_leader.hotkey[:8]}...` — `{new_leader.repo}@{new_leader.commit[:8]}`",
                    "inline": False,
                },
            ],
        )

    async def notify_competition_ended(self, round_num: int) -> None:
        await self._webhook.send_embed(
            title="Competition Ended",
            color=0x9B59B6,
            description=f"Round {round_num} was the final round. No more rounds will be scheduled.",
        )

    async def notify_cycle_error(self, error: Exception) -> None:
        await self._webhook.send_embed(
            title="Round Manager Error",
            color=0xE74C3C,
            description=f"```{type(error).__name__}: {error}```",
        )

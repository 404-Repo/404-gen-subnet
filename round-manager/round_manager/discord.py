from datetime import datetime

from subnet_common.competition.leader import LeaderEntry
from subnet_common.discord import DiscordWebhook


def _commit_url(repo: str, commit: str) -> str:
    return f"https://github.com/{repo}/commit/{commit}"


def _leader_value(leader: LeaderEntry) -> str:
    short = leader.commit[:8]
    url = _commit_url(leader.repo, leader.commit)
    return f"[{leader.repo}@{short}]({url}) · weight: {leader.weight}\n||{leader.hotkey}||"


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook = DiscordWebhook(webhook_url)

    async def notify_round_finalized(
        self,
        completed_round: int,
        next_round: int,
        next_round_start: datetime,
        leader: LeaderEntry,
    ) -> None:
        unix_ts = int(next_round_start.timestamp())
        await self._webhook.send_embed(
            title=f"Round {completed_round} Finalized",
            color=0x2ECC71,
            fields=[
                {"name": "Next Round", "value": str(next_round), "inline": True},
                {"name": "Next Round Start", "value": f"<t:{unix_ts}:f>", "inline": True},
                {"name": "Current Leader", "value": _leader_value(leader), "inline": False},
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
                {"name": "Previous Leader", "value": _leader_value(old_leader), "inline": False},
                {"name": "New Leader", "value": _leader_value(new_leader), "inline": False},
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


class NullDiscordNotifier(DiscordNotifier):
    def __init__(self) -> None:
        pass

    async def notify_round_finalized(
        self,
        completed_round: int,
        next_round: int,
        next_round_start: datetime,
        leader: LeaderEntry,
    ) -> None:
        pass

    async def notify_leader_change(self, old_leader: LeaderEntry, new_leader: LeaderEntry, round_num: int) -> None:
        pass

    async def notify_competition_ended(self, round_num: int) -> None:
        pass

    async def notify_cycle_error(self, error: Exception) -> None:
        pass


NULL_DISCORD_NOTIFIER = NullDiscordNotifier()

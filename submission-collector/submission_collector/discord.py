from datetime import datetime

from subnet_common.competition.generations import GenerationsMap
from subnet_common.discord import DiscordWebhook


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook = DiscordWebhook(webhook_url)

    async def notify_submissions_collected(
        self, round_num: int, submission_count: int, generation_deadline: datetime
    ) -> None:
        unix_ts = int(generation_deadline.timestamp())
        await self._webhook.send_embed(
            title=f"Round {round_num} Submissions Collected",
            color=0x2ECC71,
            fields=[
                {"name": "Round", "value": str(round_num), "inline": True},
                {"name": "Submissions", "value": str(submission_count), "inline": True},
                {"name": "Generation Deadline", "value": f"<t:{unix_ts}:f>", "inline": True},
            ],
        )

    async def notify_no_submissions(self, round_num: int) -> None:
        await self._webhook.send_embed(
            title=f"Round {round_num} — No Submissions",
            color=0xE74C3C,
            description=f"Round {round_num} received no valid submissions. Transitioning to FINALIZING.",
        )

    async def notify_downloads_complete(self, round_num: int, results: dict[str, GenerationsMap]) -> None:
        prompt_count = max((len(v) for v in results.values()), default=0)
        none = sum(1 for gens in results.values() if not any(g.glb is not None for g in gens.values()))
        full = sum(
            1 for gens in results.values() if 0 < prompt_count == sum(1 for g in gens.values() if g.glb is not None)
        )
        partial = len(results) - full - none
        await self._webhook.send_embed(
            title=f"Round {round_num} Downloads Complete",
            color=0x2ECC71,
            fields=[
                {"name": "Full", "value": str(full), "inline": True},
                {"name": "Partial", "value": str(partial), "inline": True},
                {"name": "None", "value": str(none), "inline": True},
            ],
        )

    async def notify_cycle_error(self, error: Exception) -> None:
        await self._webhook.send_embed(
            title="Submission Collector Error",
            color=0xE74C3C,
            description=f"```{type(error).__name__}: {error}```",
        )

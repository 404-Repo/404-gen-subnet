from datetime import UTC, datetime

from subnet_common.competition.generations import GenerationsMap
from subnet_common.discord import DiscordWebhook


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str,
        render_alert_min_failures: int = 20,
        render_alert_min_hotkeys: int = 3,
        render_alert_cooldown_seconds: int = 600,
    ) -> None:
        self._webhook = DiscordWebhook(webhook_url)
        self._render_alert_min_failures = render_alert_min_failures
        self._render_alert_min_hotkeys = render_alert_min_hotkeys
        self._render_alert_cooldown_seconds = render_alert_cooldown_seconds
        self._render_failures = 0
        self._render_failure_hotkeys: set[str] = set()
        self._last_render_alert: datetime | None = None

    async def notify_submissions_collected(
        self,
        round_num: int,
        submission_count: int,
        generation_deadline: datetime,
        prompts_url: str,
        seed_url: str,
    ) -> None:
        unix_ts = int(generation_deadline.timestamp())
        await self._webhook.send_embed(
            title=f"Round {round_num} Submissions Collected",
            color=0x2ECC71,
            fields=[
                {"name": "Round", "value": str(round_num), "inline": True},
                {"name": "Submissions", "value": str(submission_count), "inline": True},
                {"name": "Generation Deadline", "value": f"<t:{unix_ts}:f>", "inline": True},
                {"name": "Prompts", "value": f"[prompts.txt]({prompts_url})", "inline": True},
                {"name": "Seed", "value": f"[seed.json]({seed_url})", "inline": True},
            ],
        )

    async def notify_no_submissions(self, round_num: int) -> None:
        await self._webhook.send_embed(
            title=f"Round {round_num} — No Submissions",
            color=0xE74C3C,
            description=f"Round {round_num} received no valid submissions. Transitioning to FINALIZING.",
        )

    async def notify_render_failure(self, hotkey: str) -> None:
        self._render_failures += 1
        self._render_failure_hotkeys.add(hotkey)
        if self._render_failures < self._render_alert_min_failures:
            return
        if len(self._render_failure_hotkeys) < self._render_alert_min_hotkeys:
            return
        now = datetime.now(UTC)
        if (
            self._last_render_alert
            and (now - self._last_render_alert).total_seconds() < self._render_alert_cooldown_seconds
        ):
            return
        self._last_render_alert = now
        await self._webhook.send_embed(
            title="Render Failures Detected",
            color=0xE74C3C,
            description=f"{self._render_failures} render failures across {len(self._render_failure_hotkeys)} miners. "
            f"Render service may be down.",
        )

    async def notify_downloads_complete(self, round_num: int, results: dict[str, GenerationsMap]) -> None:
        all_gens = [g for gens in results.values() for g in gens.values()]
        complete = sum(1 for g in all_gens if g.glb is not None and g.png is not None)
        no_png = sum(1 for g in all_gens if g.glb is not None and g.png is None)
        failed = sum(1 for g in all_gens if g.glb is None)
        await self._webhook.send_embed(
            title=f"Round {round_num} Downloads Complete",
            color=0x2ECC71,
            fields=[
                {"name": "Complete", "value": str(complete), "inline": True},
                {"name": "No PNG", "value": str(no_png), "inline": True},
                {"name": "Failed", "value": str(failed), "inline": True},
            ],
        )

    async def notify_cycle_error(self, error: Exception) -> None:
        await self._webhook.send_embed(
            title="Submission Collector Error",
            color=0xE74C3C,
            description=f"```{type(error).__name__}: {error}```",
        )


class NullDiscordNotifier(DiscordNotifier):
    def __init__(self) -> None:
        pass

    async def notify_submissions_collected(
        self, round_num: int, submission_count: int, generation_deadline: datetime, prompts_url: str, seed_url: str
    ) -> None:
        pass

    async def notify_no_submissions(self, round_num: int) -> None:
        pass

    async def notify_render_failure(self, hotkey: str) -> None:
        pass

    async def notify_downloads_complete(self, round_num: int, results: dict[str, GenerationsMap]) -> None:
        pass

    async def notify_cycle_error(self, error: Exception) -> None:
        pass


NULL_DISCORD_NOTIFIER = NullDiscordNotifier()

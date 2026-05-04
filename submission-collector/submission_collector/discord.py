from datetime import UTC, datetime

from subnet_common.competition.generations import GenerationsMap
from subnet_common.discord import DiscordWebhook


COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C


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
            color=COLOR_GREEN,
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
            color=COLOR_RED,
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
            color=COLOR_RED,
            description=f"{self._render_failures} render failures across {len(self._render_failure_hotkeys)} miners. "
            f"Render service may be down.",
        )

    async def notify_downloads_complete(self, round_num: int, results: dict[str, GenerationsMap]) -> None:
        total_miners = len(results)
        if total_miners == 0:
            await self._webhook.send_embed(
                title=f"Round {round_num} Downloads Complete",
                color=COLOR_GREEN,
                fields=[{"name": "Miners", "value": "0", "inline": True}],
            )
            return

        # Total prompts inferred from the largest GenerationsMap. Miners that crashed before
        # completing their batch will have fewer entries; "perfect" requires the full count.
        total_prompts = max((len(gens) for gens in results.values()), default=0)

        perfect = sum(
            1
            for gens in results.values()
            if total_prompts > 0 and len(gens) >= total_prompts and all(g.js is not None for g in gens.values())
        )

        # Miners with no successfully uploaded JS for any prompt. Every entry persisted
        # by the pipeline carries either a JS URL or a failure_reason, so this counts
        # miners whose every prompt failed before the JS upload (dead CDN, oversized
        # JS, R2 reject, etc.) — i.e., they delivered nothing usable this round.
        no_submission = sum(1 for gens in results.values() if not any(g.js is not None for g in gens.values()))

        # Stems where the miner delivered JS but our render/embedding pipeline failed to
        # produce views. This is *our* problem to act on — every other number is
        # informational about miner behavior.
        render_failures = sum(
            1 for gens in results.values() for g in gens.values() if g.js is not None and g.views is None
        )

        # Red if anything on the system side is actionable; green otherwise.
        color = COLOR_RED if render_failures > 0 else COLOR_GREEN

        await self._webhook.send_embed(
            title=f"Round {round_num} Downloads Complete",
            color=color,
            fields=[
                {"name": "Miners", "value": str(total_miners), "inline": True},
                {
                    "name": "Perfect (100%)",
                    "value": f"{perfect} ({100 * perfect // total_miners}%)",
                    "inline": True,
                },
                {
                    "name": "No submission",
                    "value": f"{no_submission} ({100 * no_submission // total_miners}%)",
                    "inline": True,
                },
                {"name": "Render failures", "value": str(render_failures), "inline": True},
            ],
        )

        self._render_failures = 0
        self._render_failure_hotkeys.clear()
        self._last_render_alert = None

    async def notify_cycle_error(self, error: Exception) -> None:
        await self._webhook.send_embed(
            title="Submission Collector Error",
            color=COLOR_RED,
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

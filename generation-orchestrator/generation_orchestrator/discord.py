from subnet_common.competition.generation_report import GenerationReport, GenerationReportOutcome
from subnet_common.discord import DiscordWebhook


# TODO: For Max to review


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook = DiscordWebhook(webhook_url)

    async def notify_generation_report(self, report: GenerationReport) -> None:
        if report.outcome == GenerationReportOutcome.PENDING:
            return
        if report.outcome == GenerationReportOutcome.COMPLETED:
            gen_time = f"{report.generation_time:.1f}s" if report.generation_time is not None else "N/A"
            description = (
                f"Miner `{report.hotkey[:10]}` completed — {report.checked_prompts} prompts, median {gen_time}"
            )
            color = 0x2ECC71
        else:
            description = f"Miner `{report.hotkey[:10]}` rejected — {report.reason}"
            color = 0xE74C3C
        await self._webhook.send_embed(
            title="Generation Report",
            color=color,
            description=description,
        )

    async def notify_generation_progress(
        self,
        *,
        hotkey: str,
        round_num: int,
        generated: int,
        total: int,
        fails: int,
        total_generation_time: float,
        replacements_used: int,
    ) -> None:
        pct = generated * 100 // total if total else 0
        lines = [
            f"Generated     {generated} / {total} ({pct}%)",
            f"Fails         {fails:>3}",
            f"Total time    {total_generation_time:.1f}s",
            f"Replacements  {replacements_used}",
        ]
        await self._webhook.send_embed(
            title=f"Round {round_num} Generation — `{hotkey[:10]}`",
            color=0x2ECC71,
            description="```\n" + "\n".join(lines) + "\n```",
        )

    async def notify_pod_warmup_failed(self, hotkey: str, round_num: int) -> None:
        await self._webhook.send_embed(
            title=f"Round {round_num} Pod Warmup Failed",
            color=0xE74C3C,
            description=f"All initial pods failed warmup for `{hotkey[:10]}`",
        )

    async def notify_pod_bad(self, hotkey: str, pod_id: str, provider: str, reason: str) -> None:
        await self._webhook.send_embed(
            title="Pod Marked Bad",
            color=0xE67E22,
            description=f"`{pod_id}` on {provider}: {reason}",
        )

    async def notify_pod_deploy_failed(self, hotkey: str, pod_id: str) -> None:
        await self._webhook.send_embed(
            title="Pod Deploy Failed",
            color=0xE74C3C,
            description=f"`{hotkey[:10]}` / `{pod_id}` — failed to get healthy pod",
        )

    async def notify_batch_submit_failed(self, hotkey: str, pod_id: str) -> None:
        await self._webhook.send_embed(
            title="Batch Submission Failed",
            color=0xE74C3C,
            description=f"`{hotkey[:10]}` / `{pod_id}` — pod rejected the batch (HTTP error / timeout); "
            f"see logs for status code",
        )

    async def notify_batch_download_failed(self, hotkey: str, pod_id: str) -> None:
        await self._webhook.send_embed(
            title="Batch Download Failed",
            color=0xE74C3C,
            description=f"`{hotkey[:10]}` / `{pod_id}` — could not download results after batch completed",
        )

    async def notify_pod_unreachable(self, hotkey: str, pod_id: str) -> None:
        await self._webhook.send_embed(
            title="Pod Unreachable",
            color=0xE74C3C,
            description=f"`{hotkey[:10]}` / `{pod_id}` — `/status` unreachable beyond consecutive-failure threshold; "
            f"replacing",
        )

    async def notify_cycle_error(self, error: Exception) -> None:
        await self._webhook.send_embed(
            title="Generation Orchestrator Error",
            color=0xE74C3C,
            description=f"```{type(error).__name__}: {error}```",
        )


class NullDiscordNotifier(DiscordNotifier):
    def __init__(self) -> None:
        pass

    async def notify_generation_report(self, report: GenerationReport) -> None:
        pass

    async def notify_generation_progress(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_pod_warmup_failed(self, hotkey: str, round_num: int) -> None:
        pass

    async def notify_pod_bad(self, hotkey: str, pod_id: str, provider: str, reason: str) -> None:
        pass

    async def notify_pod_deploy_failed(self, hotkey: str, pod_id: str) -> None:
        pass

    async def notify_batch_submit_failed(self, hotkey: str, pod_id: str) -> None:
        pass

    async def notify_batch_download_failed(self, hotkey: str, pod_id: str) -> None:
        pass

    async def notify_pod_unreachable(self, hotkey: str, pod_id: str) -> None:
        pass

    async def notify_cycle_error(self, error: Exception) -> None:
        pass


NULL_DISCORD_NOTIFIER = NullDiscordNotifier()

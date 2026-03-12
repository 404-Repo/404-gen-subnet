from subnet_common.competition.generation_audit import GenerationAudit, GenerationAuditOutcome
from subnet_common.discord import DiscordWebhook


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook = DiscordWebhook(webhook_url)

    async def notify_miner_audit(self, audit: GenerationAudit) -> None:
        if audit.outcome == GenerationAuditOutcome.PENDING:
            return
        if audit.outcome == GenerationAuditOutcome.PASSED:
            gen_time = f"{audit.generation_time:.1f}s" if audit.generation_time is not None else "N/A"
            description = f"Miner `{audit.hotkey[:10]}` passed — {audit.checked_prompts} prompts, median {gen_time}"
            color = 0x2ECC71
        else:
            description = f"Miner `{audit.hotkey[:10]}` rejected — {audit.reason}"
            color = 0xE74C3C
        await self._webhook.send_embed(
            title="Miner Audit Result",
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
        final_fails: int,
        overtimes: int,
        final_overtimes: int,
        mismatches: int,
        median_time: float | None,
    ) -> None:
        median = f"{median_time:.1f}s" if median_time is not None else "N/A"
        pct = generated * 100 // total if total else 0
        lines = [
            f"Generated    {generated} / {total} ({pct}%)",
            f"Fails        {fails:>3}  final: {final_fails}",
            f"Overtimes    {overtimes:>3}  final: {final_overtimes}",
            f"Mismatches   {mismatches:>3}",
            f"Median time  {median}",
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

    async def notify_cycle_error(self, error: Exception) -> None:
        await self._webhook.send_embed(
            title="Generation Orchestrator Error",
            color=0xE74C3C,
            description=f"```{type(error).__name__}: {error}```",
        )


class NullDiscordNotifier(DiscordNotifier):
    def __init__(self) -> None:
        pass

    async def notify_miner_audit(self, audit: GenerationAudit) -> None:
        pass

    async def notify_generation_progress(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    async def notify_pod_warmup_failed(self, hotkey: str, round_num: int) -> None:
        pass

    async def notify_pod_bad(self, hotkey: str, pod_id: str, provider: str, reason: str) -> None:
        pass

    async def notify_pod_deploy_failed(self, hotkey: str, pod_id: str) -> None:
        pass

    async def notify_cycle_error(self, error: Exception) -> None:
        pass


NULL_DISCORD_NOTIFIER = NullDiscordNotifier()

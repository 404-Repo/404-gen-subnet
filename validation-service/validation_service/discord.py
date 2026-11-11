from dataclasses import dataclass
from time import monotonic

from subnet_common.competition.validation import ValidationVerdict
from subnet_common.discord import DiscordWebhook

from validation_service.execution import ValidationOutcome


PROGRESS_INTERVAL_SECONDS = 20 * 60


@dataclass
class _CandidateNotifications:
    started: bool = False
    passed_reported: int = 0
    blocked: bool = False


class ValidationNotifier:
    def __init__(self, webhook: DiscordWebhook) -> None:
        self._webhook = webhook
        self._round_num: int | None = None
        self._candidates: dict[str, _CandidateNotifications] = {}
        self._next_progress_at = float("-inf")

    def _candidate(self, round_num: int, candidate: str) -> _CandidateNotifications:
        if self._round_num != round_num:
            self._round_num = round_num
            self._candidates.clear()
        return self._candidates.setdefault(candidate, _CandidateNotifications())

    async def notify_started(self, round_num: int, candidate: str) -> None:
        status = self._candidate(round_num, candidate)
        if status.started:
            return
        status.started = True
        self._next_progress_at = monotonic() + PROGRESS_INTERVAL_SECONDS
        await self._webhook.send_embed(
            title=f"Round {round_num} Validation Started",
            color=0x3498DB,
            description=f"Candidate `{candidate}`",
        )

    async def notify_pending(self, round_num: int, candidate: str, outcome: ValidationOutcome) -> None:
        status = self._candidate(round_num, candidate)
        counts = f"Candidate `{candidate}` has passed **{outcome.passed_prompts}/{outcome.total_prompts}** prompts."
        if not outcome.has_work:
            if status.blocked:
                return
            status.blocked = True
            await self._webhook.send_embed(
                title=f"Round {round_num} Validation Blocked",
                color=0xE74C3C,
                description=f"{counts}\nNo automatic attempts remain. Check the worker logs and committed checkpoint.",
            )
            return
        status.blocked = False
        now = monotonic()
        if outcome.passed_prompts <= status.passed_reported or now < self._next_progress_at:
            return
        status.passed_reported = outcome.passed_prompts
        self._next_progress_at = now + PROGRESS_INTERVAL_SECONDS
        await self._webhook.send_embed(
            title=f"Round {round_num} Validation Progress",
            color=0x3498DB,
            description=counts,
        )

    async def notify_result(self, round_num: int, candidate: str, verdict: ValidationVerdict) -> None:
        await self._webhook.send_embed(
            title=f"Round {round_num} Validation {verdict.value.title()}",
            color=0x2ECC71 if verdict == ValidationVerdict.PASSED else 0xE74C3C,
            description=f"Candidate `{candidate}`",
        )

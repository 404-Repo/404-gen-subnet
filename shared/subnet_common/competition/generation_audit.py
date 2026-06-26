from pydantic import BaseModel, TypeAdapter

from subnet_common.competition.source_audit import AuditVerdict
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class GenerationAuditResult(BaseModel):
    """Verification-audit verdict for a miner, written by judge-service once the
    submitted-vs-generated / defender-vs-generated duels reach a terminal outcome.

    Read by the generation orchestrator to stop regenerating a miner that has already
    failed verification.
    """

    hotkey: str
    verdict: AuditVerdict
    reason: str = ""


GenerationAuditsAdapter = TypeAdapter(dict[str, GenerationAuditResult])


async def get_generation_audits(git: GitHubClient, round_num: int, ref: str) -> dict[str, GenerationAuditResult]:
    content = await git.get_file(f"rounds/{round_num}/generation_audits.json", ref=ref)
    if not content:
        return {}
    return GenerationAuditsAdapter.validate_json(content)


async def save_generation_audits(
    git_batcher: GitBatcher, round_num: int, audits: dict[str, GenerationAuditResult]
) -> None:
    await git_batcher.write(
        path=f"rounds/{round_num}/generation_audits.json",
        content=GenerationAuditsAdapter.dump_json(audits, indent=2).decode(),
        message=f"Update generation audits for round {round_num}",
    )

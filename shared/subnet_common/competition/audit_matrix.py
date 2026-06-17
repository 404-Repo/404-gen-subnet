"""Audit results storage. Reuses MatchMatrix.

Row conventions:
- left = "submitted" — generated-vs-submitted audit
- left = <defender hotkey> — generated-vs-<defender> audit
- right = audited hotkey
"""

from subnet_common.competition.match_matrix import MatchMatrix
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


async def get_audit_matrix(git: GitHubClient, round_num: int, ref: str) -> MatchMatrix:
    content = await git.get_file(f"rounds/{round_num}/audit_matrix.csv", ref=ref)
    if not content:
        return MatchMatrix()
    return MatchMatrix.from_csv(content)


async def save_audit_matrix(git_batcher: GitBatcher, round_num: int, matrix: MatchMatrix) -> None:
    await git_batcher.write(
        path=f"rounds/{round_num}/audit_matrix.csv",
        content=matrix.to_csv(),
        message=f"Update audit matrix for round {round_num}",
    )

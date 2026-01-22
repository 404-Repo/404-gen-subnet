from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class MatchMatrix:
    """
    Match results storage: (left, right) -> margin.

    Convention:
    - left: defender (current leader in the match)
    - right: challenger (attempting to beat the leader)
    - margin > 0: challenger (right) wins
    - margin < 0: defender (left) wins
    - margin >= threshold: challenger becomes new leader
    """

    def __init__(self) -> None:
        self._margins: dict[tuple[str, str], float] = {}

    def add(self, left: str, right: str, margin: float) -> None:
        self._margins[(left, right)] = margin

    def has(self, left: str, right: str) -> bool:
        return (left, right) in self._margins

    def get(self, left: str, right: str) -> float | None:
        return self._margins.get((left, right))

    def __len__(self) -> int:
        return len(self._margins)

    def to_csv(self) -> str:
        lines = ["left,right,margin"]
        for (left, right), margin in self._margins.items():
            lines.append(f"{left},{right},{margin}")
        return "\n".join(lines)

    @classmethod
    def from_csv(cls, content: str) -> "MatchMatrix":
        matrix = cls()
        lines = content.strip().split("\n")
        if len(lines) <= 1:
            return matrix

        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) >= 3:
                matrix.add(parts[0], parts[1], float(parts[2]))

        return matrix


async def get_match_matrix(git: GitHubClient, round_num: int, ref: str) -> MatchMatrix:
    # TODO: save decisive prompts or re-load them from duels.
    raise RuntimeError("Not implemented")

    content = await git.get_file(f"rounds/{round_num}/matches_matrix.csv", ref=ref)
    if not content:
        return MatchMatrix()
    return MatchMatrix.from_csv(content)


async def save_match_matrix(git_batcher: GitBatcher, round_num: int, matrix: MatchMatrix) -> None:
    await git_batcher.write(
        path=f"rounds/{round_num}/matches_matrix.csv",
        content=matrix.to_csv(),
        message=f"Update matches matrix for round {round_num}",
    )

from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.github import GitHubClient


class LeaderEntry(BaseModel):
    hotkey: str = Field(description="Miner hotkey")
    repo: str = Field(description="GitHub repo")
    commit: str = Field(description="Git commit SHA")
    docker: str = Field(description="Docker image")
    weight: float = Field(description="Weight set by validators")
    effective_block: int = Field(description="Block when this leader becomes active")


class LeaderState(BaseModel):
    transitions: list[LeaderEntry] = Field(default_factory=list, description="Leadership history, append-only")

    def get_leader(self, current_block: int) -> LeaderEntry | None:
        """Returns the active leader at the given block."""
        for entry in reversed(self.transitions):
            if current_block >= entry.effective_block:
                return entry
        return None

    def get_latest(self) -> LeaderEntry | None:
        """Returns the latest leader transition."""
        return self.transitions[-1] if self.transitions else None

    def append(self, entry: LeaderEntry) -> None:
        """Append a new leader transition."""
        if self.transitions and entry.effective_block <= self.transitions[-1].effective_block:
            raise ValueError("New entry must have a later effective_block")
        self.transitions.append(entry)


LeaderListAdapter = TypeAdapter(list[LeaderEntry])


async def require_leader_state(git: GitHubClient, ref: str) -> LeaderState:
    content = await git.get_file("leader.json", ref=ref)
    if content is None:
        raise FileNotFoundError("leader.json not found")
    transitions = LeaderListAdapter.validate_json(content)
    return LeaderState(transitions=transitions)

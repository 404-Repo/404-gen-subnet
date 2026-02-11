import json

import pytest
from pydantic import SecretStr
from subnet_common.competition.config import CompetitionConfig
from subnet_common.competition.schedule import RoundSchedule
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.git_batcher import GitBatcher
from subnet_common.testing import MockGitHubClient

from submission_collector.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        GITHUB_TOKEN=SecretStr("test-token"),
        GITHUB_REPO="test/repo",
        R2_ACCESS_KEY_ID=SecretStr("test-r2-key"),
        R2_SECRET_ACCESS_KEY=SecretStr("test-r2-secret"),
        R2_ENDPOINT=SecretStr("https://test-r2-endpoint"),
        DOWNLOAD_JITTER_SECONDS=0,
    )


@pytest.fixture
def git() -> MockGitHubClient:
    return MockGitHubClient()


def add_state(git: MockGitHubClient, stage: RoundStage, round_num: int = 1, **kwargs) -> CompetitionState:
    """Create a CompetitionState and store it in the mock git."""
    state = CompetitionState(current_round=round_num, stage=stage, **kwargs)
    git.files["state.json"] = state.model_dump_json()
    return state


def add_schedule(
    git: MockGitHubClient,
    round_num: int = 1,
    earliest_reveal_block: int = 100,
    latest_reveal_block: int = 200,
    generation_deadline_block: int = 500,
) -> RoundSchedule:
    """Create a RoundSchedule and store it in the mock git."""
    schedule = RoundSchedule(
        earliest_reveal_block=earliest_reveal_block,
        latest_reveal_block=latest_reveal_block,
        generation_deadline_block=generation_deadline_block,
    )
    git.files[f"rounds/{round_num}/schedule.json"] = schedule.model_dump_json()
    return schedule


def add_config(git: MockGitHubClient, **kwargs) -> CompetitionConfig:
    """Create a CompetitionConfig and store it in the mock git."""
    defaults = dict(
        name="test-comp",
        description="Test competition",
        first_evaluation_date="2025-01-01",
        last_competition_date="2025-12-31",
        generation_stage_minutes=180,
        win_margin=0.05,
        weight_decay=0.1,
        weight_floor=0.1,
        prompts_per_round=3,
        carryover_prompts=0,
    )
    defaults.update(kwargs)
    config = CompetitionConfig(**defaults)
    git.files["config.json"] = config.model_dump_json()
    return config


def add_prompts(git: MockGitHubClient, prompts: list[str], round_num: int | None = None) -> None:
    """Store prompts in the mock git (global pool if round_num is None)."""
    path = "prompts.txt" if round_num is None else f"rounds/{round_num}/prompts.txt"
    git.files[path] = "\n".join(prompts)


def make_commitment(
    repo: str = "test/model",
    commit: str = "a" * 40,
    cdn_url: str = "https://cdn.example.com/files",
    block: int = 150,
) -> tuple[tuple[int, str], ...]:
    """Create a single commitment tuple as returned by subtensor."""
    data = json.dumps({"repo": repo, "commit": commit, "cdn_url": cdn_url})
    return ((block, data),)


def make_get_block(block: int = 7200):
    """Factory to create a controllable get_block function."""

    async def get_block() -> int:
        return block

    return get_block


def make_get_commitments(commitments: dict | None = None):
    """Factory to create a controllable get_commitments function."""

    async def get_commitments() -> dict:
        return commitments or {}

    return get_commitments


class SpyDownloadFn:
    """A download_fn that tracks whether it was called."""

    def __init__(self) -> None:
        self.called: bool = False

    async def __call__(
        self,
        git_batcher: GitBatcher,
        state: CompetitionState,
        ref: str,
        settings: Settings,
    ) -> None:
        self.called = True

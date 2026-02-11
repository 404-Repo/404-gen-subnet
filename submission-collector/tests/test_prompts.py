import pytest
from subnet_common.competition.config import CompetitionConfig
from subnet_common.testing import MockGitHubClient

from submission_collector.prompts import select_prompts
from tests.conftest import add_prompts


REF = "abc123"
POOL = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]


def _config(prompts_per_round: int = 3, carryover_prompts: int = 0, **kwargs) -> CompetitionConfig:
    """Create a CompetitionConfig with prompt-related overrides."""
    defaults = dict(
        name="test",
        description="test",
        first_evaluation_date="2025-01-01",
        last_competition_date="2025-12-31",
        generation_stage_minutes=180,
        win_margin=0.05,
        weight_decay=0.1,
        weight_floor=0.1,
        prompts_per_round=prompts_per_round,
        carryover_prompts=carryover_prompts,
    )
    defaults.update(kwargs)
    return CompetitionConfig(**defaults)


async def test_selects_correct_number_of_prompts(git: MockGitHubClient) -> None:
    add_prompts(git, POOL)
    config = _config(prompts_per_round=3)

    result = await select_prompts(git=git, round_num=0, config=config, seed=42, ref=REF)

    assert len(result) == 3
    assert all(p in POOL for p in result)


async def test_deterministic_with_same_seed(git: MockGitHubClient) -> None:
    add_prompts(git, POOL)
    config = _config(prompts_per_round=3)

    result1 = await select_prompts(git=git, round_num=0, config=config, seed=42, ref=REF)
    result2 = await select_prompts(git=git, round_num=0, config=config, seed=42, ref=REF)

    assert result1 == result2


async def test_different_seed_gives_different_prompts(git: MockGitHubClient) -> None:
    add_prompts(git, POOL)
    config = _config(prompts_per_round=3)

    result1 = await select_prompts(git=git, round_num=0, config=config, seed=1, ref=REF)
    result2 = await select_prompts(git=git, round_num=0, config=config, seed=999, ref=REF)

    assert result1 != result2


async def test_round_zero_has_no_carryover(git: MockGitHubClient) -> None:
    add_prompts(git, POOL)
    config = _config(prompts_per_round=3, carryover_prompts=2)

    result = await select_prompts(git=git, round_num=0, config=config, seed=42, ref=REF)

    # All 3 should come from the pool (no previous round)
    assert len(result) == 3
    assert all(p in POOL for p in result)


async def test_carryover_from_previous_round(git: MockGitHubClient) -> None:
    add_prompts(git, POOL)
    previous = ["x_prev_1", "x_prev_2", "x_prev_3"]
    add_prompts(git, previous, round_num=0)
    config = _config(prompts_per_round=4, carryover_prompts=2)

    result = await select_prompts(git=git, round_num=1, config=config, seed=42, ref=REF)

    assert len(result) == 4
    # First part should be carryover from previous round
    carryover = [p for p in result if p in previous]
    new = [p for p in result if p in POOL]
    assert len(carryover) == 2
    assert len(new) == 2


async def test_carryover_capped_by_previous_round_size(git: MockGitHubClient) -> None:
    """If previous round had fewer prompts than carryover_prompts, take all of them."""
    add_prompts(git, POOL)
    add_prompts(git, ["only_one"], round_num=0)
    config = _config(prompts_per_round=3, carryover_prompts=5)

    result = await select_prompts(git=git, round_num=1, config=config, seed=42, ref=REF)

    assert len(result) == 3
    assert "only_one" in result


async def test_pool_smaller_than_requested(git: MockGitHubClient) -> None:
    """If pool has fewer prompts than prompts_per_round, return all available."""
    add_prompts(git, ["a", "b"])
    config = _config(prompts_per_round=5)

    result = await select_prompts(git=git, round_num=0, config=config, seed=42, ref=REF)

    assert result == ["a", "b"]


async def test_missing_global_pool_raises(git: MockGitHubClient) -> None:
    config = _config(prompts_per_round=3)

    with pytest.raises(FileNotFoundError, match="prompts.txt"):
        await select_prompts(git=git, round_num=0, config=config, seed=42, ref=REF)


async def test_missing_previous_round_prompts_raises(git: MockGitHubClient) -> None:
    add_prompts(git, POOL)
    config = _config(prompts_per_round=3, carryover_prompts=2)

    with pytest.raises(FileNotFoundError, match="rounds/0/prompts.txt"):
        await select_prompts(git=git, round_num=1, config=config, seed=42, ref=REF)


async def test_zero_carryover_skips_previous_round_fetch(git: MockGitHubClient) -> None:
    """With carryover_prompts=0, previous round prompts are not fetched."""
    add_prompts(git, POOL)
    # No previous round prompts in git -- should not raise
    config = _config(prompts_per_round=3, carryover_prompts=0)

    result = await select_prompts(git=git, round_num=1, config=config, seed=42, ref=REF)

    assert len(result) == 3

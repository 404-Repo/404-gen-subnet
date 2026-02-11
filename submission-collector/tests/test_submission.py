import json

import pytest
from pydantic import HttpUrl

from submission_collector.submission import Submission, parse_commitment


HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
EARLIEST = 100
LATEST = 200


def _commitment(data: dict | str, block: int = 150) -> tuple[tuple[int, str], ...]:
    """Build a single-entry commitment tuple."""
    raw = data if isinstance(data, str) else json.dumps(data)
    return ((block, raw),)


def _valid_data(**overrides: str) -> dict:
    defaults = {
        "repo": "test/model",
        "commit": "a" * 40,
        "cdn_url": "https://cdn.example.com/files",
    }
    defaults.update(overrides)
    return defaults


def test_valid_commitment_returns_submission() -> None:
    result = parse_commitment(
        hotkey=HOTKEY,
        commitment=_commitment(_valid_data()),
        earliest_block=EARLIEST,
        latest_block=LATEST,
    )

    assert result == Submission(
        hotkey=HOTKEY,
        repo="test/model",
        commit="a" * 40,
        cdn_url=HttpUrl("https://cdn.example.com/files"),
        reveal_block=150,
    )


def test_multiple_commits_uses_latest_value() -> None:
    commitment = (
        (110, json.dumps({"repo": "old/repo", "commit": "b" * 40, "cdn_url": "https://old.example.com"})),
        (190, json.dumps({"repo": "new/repo", "commit": "c" * 40, "cdn_url": "https://new.example.com"})),
    )

    result = parse_commitment(hotkey=HOTKEY, commitment=commitment, earliest_block=EARLIEST, latest_block=LATEST)

    assert result is not None
    assert result.repo == "new/repo"
    assert result.commit == "c" * 40
    assert str(result.cdn_url).rstrip("/") == "https://new.example.com"


def test_fields_across_separate_commits() -> None:
    """Miners can submit repo, commit, cdn_url in different commits."""
    commitment = (
        (110, json.dumps({"repo": "test/model"})),
        (120, json.dumps({"commit": "d" * 40})),
        (130, json.dumps({"cdn_url": "https://cdn.example.com"})),
    )

    result = parse_commitment(hotkey=HOTKEY, commitment=commitment, earliest_block=EARLIEST, latest_block=LATEST)

    assert result is not None
    assert result.repo == "test/model"
    assert result.commit == "d" * 40


def test_single_quotes_in_json_handled() -> None:
    """The parser replaces single quotes with double quotes."""
    raw = "{'repo': 'test/model', 'commit': '" + "a" * 40 + "', 'cdn_url': 'https://cdn.example.com'}"
    result = parse_commitment(
        hotkey=HOTKEY,
        commitment=((150, raw),),
        earliest_block=EARLIEST,
        latest_block=LATEST,
    )

    assert result is not None
    assert result.repo == "test/model"


def test_commit_before_window_ignored() -> None:
    result = parse_commitment(
        hotkey=HOTKEY,
        commitment=_commitment(_valid_data(), block=50),
        earliest_block=EARLIEST,
        latest_block=LATEST,
    )

    assert result is None


def test_commit_after_window_ignored() -> None:
    result = parse_commitment(
        hotkey=HOTKEY,
        commitment=_commitment(_valid_data(), block=300),
        earliest_block=EARLIEST,
        latest_block=LATEST,
    )

    assert result is None


def test_commit_at_window_boundaries_accepted() -> None:
    for block in [EARLIEST, LATEST]:
        result = parse_commitment(
            hotkey=HOTKEY,
            commitment=_commitment(_valid_data(), block=block),
            earliest_block=EARLIEST,
            latest_block=LATEST,
        )
        assert result is not None


@pytest.mark.parametrize("missing_field", ["repo", "commit", "cdn_url"])
def test_missing_required_field_returns_none(missing_field: str) -> None:
    data = _valid_data()
    del data[missing_field]

    result = parse_commitment(hotkey=HOTKEY, commitment=_commitment(data), earliest_block=EARLIEST, latest_block=LATEST)

    assert result is None


@pytest.mark.parametrize("empty_field", ["repo", "commit", "cdn_url"])
def test_empty_field_returns_none(empty_field: str) -> None:
    data = _valid_data(**{empty_field: ""})

    result = parse_commitment(hotkey=HOTKEY, commitment=_commitment(data), earliest_block=EARLIEST, latest_block=LATEST)

    assert result is None


def test_malformed_json_returns_none() -> None:
    result = parse_commitment(
        hotkey=HOTKEY,
        commitment=((150, "not json at all"),),
        earliest_block=EARLIEST,
        latest_block=LATEST,
    )

    assert result is None


def test_invalid_repo_format_returns_none() -> None:
    """Repo must match owner/name pattern."""
    result = parse_commitment(
        hotkey=HOTKEY,
        commitment=_commitment(_valid_data(repo="no-slash")),
        earliest_block=EARLIEST,
        latest_block=LATEST,
    )

    assert result is None


def test_commit_sha_too_short_returns_none() -> None:
    result = parse_commitment(
        hotkey=HOTKEY,
        commitment=_commitment(_valid_data(commit="abc123")),
        earliest_block=EARLIEST,
        latest_block=LATEST,
    )

    assert result is None


def test_commit_sha_too_long_returns_none() -> None:
    result = parse_commitment(
        hotkey=HOTKEY,
        commitment=_commitment(_valid_data(commit="a" * 41)),
        earliest_block=EARLIEST,
        latest_block=LATEST,
    )

    assert result is None


def test_invalid_cdn_url_returns_none() -> None:
    result = parse_commitment(
        hotkey=HOTKEY,
        commitment=_commitment(_valid_data(cdn_url="not-a-url")),
        earliest_block=EARLIEST,
        latest_block=LATEST,
    )

    assert result is None


def test_short_hotkey_returns_none() -> None:
    result = parse_commitment(
        hotkey="short",
        commitment=_commitment(_valid_data()),
        earliest_block=EARLIEST,
        latest_block=LATEST,
    )

    assert result is None


def test_empty_commitment_returns_none() -> None:
    result = parse_commitment(
        hotkey=HOTKEY,
        commitment=(),
        earliest_block=EARLIEST,
        latest_block=LATEST,
    )

    assert result is None

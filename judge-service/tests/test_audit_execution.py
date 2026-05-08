from unittest.mock import AsyncMock, MagicMock, patch

from subnet_common.competition.generations import GenerationResult
from subnet_common.competition.match_report import MatchReport
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.audit_execution import (
    SUBMITTED_LEFT,
    produce_generated_vs_defender_audit,
    produce_generated_vs_submitted_audit,
)


def _gen(views: str | None = "https://cdn/preview") -> GenerationResult:
    return GenerationResult(js="https://cdn/model.js", views=views)


# Producers — both audits save through `save_match_report(kind='audit')`. These tests
# verify the wiring; per-duel score math itself is covered by `test_match_execution.py`
# (both producers route through `run_match`).


async def test_produce_generated_vs_submitted_audit_saves_audit_submitted_with_correct_sides() -> None:
    """The submitted audit saves at `rounds/{n}/{hotkey}/audit_submitted.json` with
    submitted=LEFT, generated=RIGHT. Positive margin (RIGHT win) is the legit signal."""
    write_calls: list[dict] = []

    async def _write(*, path: str, content: str, message: str) -> None:
        write_calls.append({"path": path, "content": content, "message": message})

    git_batcher = MagicMock()
    git_batcher.write = AsyncMock(side_effect=_write)

    captured: dict = {}

    async def _fake_run_match(**kwargs: object) -> MatchReport:
        captured.update(kwargs)
        return MatchReport(
            left=str(kwargs["left"]),
            right=str(kwargs["right"]),
            score=2,
            margin=0.5,
            duels=[],
        )

    with patch("judge_service.audit_execution.run_match", new=_fake_run_match):
        await produce_generated_vs_submitted_audit(
            openai=AsyncMock(),
            git_batcher=git_batcher,
            round_num=3,
            hotkey="audited_full_xxxxxxxx",
            prompts=["a.png"],
            seed=42,
            submitted_gens={"a": _gen()},
            generated_gens={"a": _gen()},
            max_concurrent_vlm_calls=2,
            max_concurrent_duels=2,
            shutdown=GracefulShutdown(),
        )

    assert captured["left"] == SUBMITTED_LEFT
    assert captured["right"] == "audited_full_xxxxxxxx"
    assert len(write_calls) == 1
    assert write_calls[0]["path"] == "rounds/3/audited_full_xxxxxxxx/audit_submitted.json"
    assert "Audit report" in write_calls[0]["message"]


async def test_produce_generated_vs_defender_audit_saves_audit_defender_with_correct_sides() -> None:
    """The defender audit saves at `rounds/{n}/{audited}/audit_{defender[:10]}.json`
    with defender=LEFT, audited.generated=RIGHT. Mirrors the submitted audit's
    direction (RIGHT = legit)."""
    write_calls: list[dict] = []

    async def _write(*, path: str, content: str, message: str) -> None:
        write_calls.append({"path": path, "content": content, "message": message})

    git_batcher = MagicMock()
    git_batcher.write = AsyncMock(side_effect=_write)

    captured: dict = {}

    async def _fake_run_match(**kwargs: object) -> MatchReport:
        captured.update(kwargs)
        return MatchReport(
            left=str(kwargs["left"]),
            right=str(kwargs["right"]),
            score=2,
            margin=0.5,
            duels=[],
        )

    with patch("judge_service.audit_execution.run_match", new=_fake_run_match):
        await produce_generated_vs_defender_audit(
            openai=AsyncMock(),
            git_batcher=git_batcher,
            round_num=7,
            audited_hotkey="audited_full_hotkey_xxxxx",
            defender_hotkey="defender_full_hotkey_yyy",
            prompts=["a.png"],
            seed=42,
            defender_gens={"a": _gen()},
            audited_generated={"a": _gen()},
            max_concurrent_vlm_calls=2,
            max_concurrent_duels=2,
            shutdown=GracefulShutdown(),
        )

    assert captured["left"] == "defender_full_hotkey_yyy"
    assert captured["right"] == "audited_full_hotkey_xxxxx"
    assert len(write_calls) == 1
    assert write_calls[0]["path"] == "rounds/7/audited_full_hotkey_xxxxx/audit_defender_f.json"
    assert "Audit report" in write_calls[0]["message"]


async def test_produce_generated_vs_defender_audit_skips_save_on_shutdown() -> None:
    """Shutdown set during run_match → returns the report without persisting."""
    git_batcher = MagicMock()
    git_batcher.write = AsyncMock()
    shutdown = GracefulShutdown()

    async def _fake_run_match(**kwargs: object) -> MatchReport:
        shutdown.request_shutdown()
        return MatchReport(left=str(kwargs["left"]), right=str(kwargs["right"]), score=0, margin=0.0, duels=[])

    with patch("judge_service.audit_execution.run_match", new=_fake_run_match):
        await produce_generated_vs_defender_audit(
            openai=AsyncMock(),
            git_batcher=git_batcher,
            round_num=1,
            audited_hotkey="audited",
            defender_hotkey="leader",
            prompts=["a.png"],
            seed=42,
            defender_gens={"a": _gen()},
            audited_generated={"a": _gen()},
            max_concurrent_vlm_calls=2,
            max_concurrent_duels=2,
            shutdown=shutdown,
        )

    git_batcher.write.assert_not_called()

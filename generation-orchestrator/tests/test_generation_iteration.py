from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from pydantic import TypeAdapter
from subnet_common.competition.audit_requests import AuditRequest
from subnet_common.competition.generation_audit import GenerationAudit, GenerationAuditOutcome
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.git_batcher import GitBatcher
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.testing import MockGitHubClient

from generation_orchestrator.generation_iteration import _generate_for_audits
from generation_orchestrator.generation_stop import GenerationStopManager
from generation_orchestrator.settings import Settings
from generation_orchestrator.staggered_semaphore import StaggeredSemaphore


HOTKEY = "5abc123def"
MODULE = "generation_orchestrator.generation_iteration"


def _audit_requests_json(*requests: AuditRequest) -> str:
    return TypeAdapter(list[AuditRequest]).dump_json(list(requests), indent=2).decode()


def _generation_audits_json(audits: dict[str, GenerationAudit]) -> str:
    return TypeAdapter(dict[str, GenerationAudit]).dump_json(audits, indent=2).decode()


async def test_audit_loop_spawns_and_saves_result(settings: Settings) -> None:
    """Core loop: audit request arrives, a task spawned, results collected and saved to git."""
    audit_request = AuditRequest(hotkey=HOTKEY, critical_prompts=["p1"])
    mock_git = MockGitHubClient(
        files={
            "rounds/1/require_audit.json": _audit_requests_json(audit_request),
        }
    )
    git_batcher = GitBatcher(git=mock_git, branch="main", base_sha="abc123")
    shutdown = GracefulShutdown()
    stop_manager = GenerationStopManager()

    state_gen = CompetitionState(current_round=1, stage=RoundStage.MINER_GENERATION)
    state_done = CompetitionState(current_round=1, stage=RoundStage.FINALIZING)

    expected_audit = GenerationAudit(
        hotkey=HOTKEY,
        outcome=GenerationAuditOutcome.PASSED,
        checked_prompts=5,
    )

    test_settings = settings.model_copy(update={"check_audit_interval_seconds": 0})

    with (
        patch(f"{MODULE}.require_state", AsyncMock(side_effect=[state_gen, state_gen, state_done])),
        patch(f"{MODULE}._generate_for_audit", AsyncMock(return_value=expected_audit)) as mock_gen,
    ):
        await _generate_for_audits(
            settings=test_settings,
            gpu_manager=AsyncMock(),
            git_batcher=git_batcher,
            build_tracker=AsyncMock(),
            semaphore=StaggeredSemaphore(value=4, delay=0),
            prompts=[],
            seed=42,
            round_num=1,
            shutdown=shutdown,
            stop_manager=stop_manager,
        )

    mock_gen.assert_called_once()
    assert mock_gen.call_args.kwargs["hotkey"] == HOTKEY

    audits_path = "rounds/1/generation_audits.json"
    assert audits_path in mock_git.committed
    saved = json.loads(mock_git.committed[audits_path])
    assert saved[HOTKEY]["outcome"] == "passed"
    assert saved[HOTKEY]["checked_prompts"] == 5


async def test_audit_loop_skips_already_completed(settings: Settings) -> None:
    """Hotkeys with non-PENDING audits are not re-processed."""
    existing_audits = {
        HOTKEY: GenerationAudit(hotkey=HOTKEY, outcome=GenerationAuditOutcome.PASSED, checked_prompts=5),
    }
    mock_git = MockGitHubClient(
        files={
            "rounds/1/require_audit.json": _audit_requests_json(AuditRequest(hotkey=HOTKEY, critical_prompts=[])),
            "rounds/1/generation_audits.json": _generation_audits_json(existing_audits),
        }
    )
    git_batcher = GitBatcher(git=mock_git, branch="main", base_sha="abc123")
    shutdown = GracefulShutdown()
    stop_manager = GenerationStopManager()

    state_gen = CompetitionState(current_round=1, stage=RoundStage.MINER_GENERATION)
    state_done = CompetitionState(current_round=1, stage=RoundStage.FINALIZING)

    test_settings = settings.model_copy(update={"check_audit_interval_seconds": 0})

    with (
        patch(f"{MODULE}.require_state", AsyncMock(side_effect=[state_gen, state_done])),
        patch(f"{MODULE}._generate_for_audit", AsyncMock()) as mock_gen,
    ):
        await _generate_for_audits(
            settings=test_settings,
            gpu_manager=AsyncMock(),
            git_batcher=git_batcher,
            build_tracker=AsyncMock(),
            semaphore=StaggeredSemaphore(value=4, delay=0),
            prompts=[],
            seed=42,
            round_num=1,
            shutdown=shutdown,
            stop_manager=stop_manager,
        )

    mock_gen.assert_not_called()
    assert not mock_git.committed

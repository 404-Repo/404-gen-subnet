"""Automatic render-error search with real replay workers and saved evidence."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from renderer_client import RenderOptions, RenderUnavailable
from replay_fixtures import SCOPE
from test_manual_replay import Stored
from test_replay_command import replay_requests

from validation_worker.manual_replay import Log, execute_prompt, recover_prompt
from validation_worker.render_recovery import RenderFailure, assumed_render_failure
from validation_worker.replay_evidence import SavedRenderer


PNG = b"\x89PNG\r\n\x1a\nsaved preview"
RID = "witness@sha256:" + "a" * 64
PRELUDE = """import httpx
from renderer_client import RenderOptions, RenderRejected, RenderUnavailable

generations = 0

async def generate(image, seed, witness):
    global generations
    generations += 1
    assert generations == 1
    options = RenderOptions(lighting="neutral")
"""
FINISH = """    await witness.render("candidate", options)
    return (await witness.responses.create(model="fixture", input="image")).output_text
"""


def run_recovery(
    directory: Path,
    code: str,
    *,
    expected: str = "0",
    preview: bytes = PNG,
    rejection: bool = False,
    max_attempts: int = 20,
    request_count: int = 2,
) -> tuple[dict[str, Any], Stored]:
    recording = replay_requests(directory, request_count=request_count)
    source = directory / "source"
    source.mkdir()
    (source / "main.py").write_text(code)
    inputs = directory / "prompts/one"
    inputs.mkdir(parents=True)
    (inputs / "image.png").write_bytes(b"PNG")
    (inputs / "expected.json").write_text(json.dumps({"available": True, "output": expected}))
    prefix = f"rounds/{SCOPE.round}/{SCOPE.hotkey}/"
    key = prefix + "renders/v1/91cf8613fc8ff82c97ef0240a975b1dbad2ebfcb2ad298be741fb9446b4de30d"
    previews = Stored({key + (".rejection.json" if rejection else ".png"): preview})
    renderer = SavedRenderer(
        previews,
        Stored({prefix + "submitted/one/grid.png": PNG + b"final"}),
        SCOPE,
        RID,
        cache=directory / "preview-cache",
        artifact_cache=directory / "final-cache",
    )

    def fetch(exchange_id: str) -> tuple[dict[str, Any], bytes]:
        item, body = recording.fetch(exchange_id)
        return item.model_dump(mode="json"), body

    log = Log(directory)

    def execute(plan: dict[str, Any], remaining: float) -> dict[str, Any]:
        return execute_prompt(
            directory=directory,
            plan=plan,
            renderer=renderer,
            final_render=lambda output: renderer.final_render("one", output, expected, prefix + "submitted/one"),
            fetch=fetch,
            log=log,
            timeout=remaining,
            image_index=recording.image_index,
            request_body=recording.request_body,
            lookup=recording.lookup,
        )

    try:
        result = recover_prompt(
            directory=directory,
            plan={
                "prompt": "one",
                "image_sha256": hashlib.sha256(b"PNG").hexdigest(),
                "seed": 42,
                "source_commit": "a" * 40,
                "renderer_id": RID,
            },
            execute=execute,
            log=log,
            timeout=30,
            max_attempts=max_attempts,
        )
    finally:
        log.close()
    assert json.loads((directory / "replays/one/summary.json").read_text())["passed"] == result["passed"]
    return result, previews


@pytest.mark.parametrize("case", ["http_retry", "different_requests", "mixed_retries"])
def test_recovers_error_sequences_without_miner_specific_rules(tmp_path: Path, case: str) -> None:
    if case == "http_retry":
        body = """    for attempt in range(3):
        try:
            await witness.render("missing", options)
        except RenderUnavailable as error:
            if getattr(getattr(error.__cause__, "response", None), "status_code", None) != 500:
                raise
"""
    else:
        second = "missing-b" if case == "different_requests" else "missing-a"
        body = """    try:
        await witness.render("missing-a", options)
    except RenderUnavailable as error:
        if getattr(getattr(error.__cause__, "response", None), "status_code", None) != 500:
            return "wrong output"
    try:
        await witness.render(SECOND, options)
    except RenderUnavailable as error:
        if not isinstance(error.__cause__, httpx.ReadTimeout):
            return "wrong output"
""".replace(
            "SECOND", repr(second)
        )
    result, _ = run_recovery(tmp_path, PRELUDE + body + FINISH)
    assert result["passed"] and result["recovered"]
    assert result["output_exact"] and result["accounting_accepted"] and result["final_render_available"]
    assert result["request_matches"]["exact"] == result["charged_calls"] == 1
    assert result["recovery_runs"] > 1
    trials = result["render_recovery"]["attempts"]
    assert not trials[0]["passed"] and trials[-1]["passed"]
    assert all(Path(trial["directory"], "summary.json").is_file() for trial in trials)
    assert (tmp_path / "replays/one/reproduced.js").read_text() == "0"
    failures = [item["failure"] for item in trials[-1]["missing_render_attempts"]]
    assert failures == (["http_500"] * 3 if case == "http_retry" else ["http_500", "timeout"])


def test_recovery_cannot_accept_a_different_output(tmp_path: Path) -> None:
    code = (
        PRELUDE
        + """    try:
        await witness.render("missing", options)
    except RenderUnavailable:
        pass
"""
        + FINISH
    )
    result, _ = run_recovery(tmp_path, code, expected="different saved output", max_attempts=3)
    assert not result["passed"] and not result["recovered"] and not result["output_exact"]
    assert result["recovery_runs"] == 3
    assert result["render_recovery"]["stopped_reason"] == "attempt_limit"
    assert (tmp_path / "replays/one/output.diff").is_file()


def test_recorded_rejections_are_not_replaced_by_assumed_errors(tmp_path: Path) -> None:
    code = (
        PRELUDE
        + """    try:
        await witness.render("candidate", options)
    except RenderRejected as error:
        assert error.details == {"error": "invalid_geometry"}
    return (await witness.responses.create(model="fixture", input="image")).output_text
"""
    )
    data = b'{"error":"invalid_geometry"}'
    result, _ = run_recovery(tmp_path, code, rejection=True, preview=data)
    assert result["passed"] and not result["recovered"] and result["recovery_runs"] == 1
    assert (tmp_path / "replays/one/renders/000.rejection.json").read_bytes() == data


def test_storage_corruption_after_a_missing_render_stops_search(tmp_path: Path) -> None:
    code = (
        PRELUDE
        + """    try:
        await witness.render("missing", options)
    except RenderUnavailable:
        pass
"""
        + FINISH
    )
    result, _ = run_recovery(tmp_path, code, preview=b"corrupted")
    assert not result["passed"] and result["failure_type"] == "EvidenceUnavailable"
    assert result["recovery_runs"] == 1


def test_an_infinite_retry_loop_is_bounded(tmp_path: Path) -> None:
    code = (
        PRELUDE
        + """    while True:
        try:
            await witness.render("missing", options)
        except RenderUnavailable:
            pass
"""
    )
    result, _ = run_recovery(tmp_path, code, max_attempts=2)
    assert not result["passed"] and result["recovery_runs"] == 2
    assert result["failure_type"] == "RenderRecoveryLimit"
    assert all(len(trial["missing_render_attempts"]) == 16 for trial in result["render_recovery"]["attempts"])


@pytest.mark.parametrize("timeout_calls", [1, 3])
def test_recovery_rejects_over_budget_trials_and_continues_search(tmp_path: Path, timeout_calls: int) -> None:
    code = (
        PRELUDE
        + """    calls = 3
    try:
        await witness.render("missing", options)
    except RenderUnavailable as error:
        if isinstance(error.__cause__, httpx.ReadTimeout):
            calls = TIMEOUT_CALLS
        elif getattr(getattr(error.__cause__, "response", None), "status_code", None) != 500:
            raise
    for request_id in range(calls):
        await witness.responses.create(request_id=request_id, model="fixture", input="image")
    return "0"
""".replace(
            "TIMEOUT_CALLS", str(timeout_calls)
        )
    )
    result, _ = run_recovery(tmp_path, code, max_attempts=3, request_count=3)
    trials = result["render_recovery"]["attempts"]
    assert result["recovery_runs"] == 3
    assert trials[1]["failure_type"] == "BudgetRejected" and not trials[1]["passed"]
    assert trials[1]["output_exact"] and trials[1]["charged_calls"] == 3
    assert trials[2]["missing_render_attempts"][0]["failure"] == "timeout"
    if timeout_calls == 1:
        assert result["passed"] and result["recovered"]
        assert result["output_exact"] and result["final_render_available"] and result["accounting_accepted"]
        assert result["request_matches"]["exact"] == result["charged_calls"] == 1
    else:
        assert not result["passed"] and not result["recovered"] and not result["accounting_accepted"]
        assert trials[2]["failure_type"] == "BudgetRejected" and not trials[2]["passed"]
        assert result["render_recovery"]["stopped_reason"] == "attempt_limit"


def test_over_budget_replay_without_missing_renders_does_not_start_search(tmp_path: Path) -> None:
    code = (
        PRELUDE
        + """    for request_id in range(3):
        await witness.responses.create(request_id=request_id, model="fixture", input="image")
    return "0"
"""
    )
    result, _ = run_recovery(tmp_path, code, request_count=3)
    assert not result["passed"] and not result["recovered"] and not result["accounting_accepted"]
    assert result["failure_type"] == "BudgetRejected" and result["charged_calls"] == 3
    assert result["recovery_runs"] == 1


@pytest.mark.parametrize("failure_type", ["EvidenceUnavailable", "ModuleNotFoundError"])
def test_recovery_does_not_hide_independent_validation_errors(tmp_path: Path, failure_type: str) -> None:
    log = Log(tmp_path)
    called = []

    def execute(plan: dict[str, Any], remaining: float) -> dict[str, Any]:
        called.append(plan)
        Path(plan["output"]).mkdir(parents=True)
        return {
            "passed": False,
            "failure_type": failure_type,
            "missing_render_attempts": [{"request_key": "missing", "occurrence": 0, "failure": "connection"}],
        }

    try:
        result = recover_prompt(directory=tmp_path, plan={"prompt": "one"}, execute=execute, log=log, timeout=30)
    finally:
        log.close()
    assert len(called) == 1 and not result["passed"]


@pytest.mark.parametrize(
    "failure,cause,status",
    [(RenderFailure.CONNECTION, httpx.ConnectError, None), (RenderFailure.TIMEOUT, httpx.ReadTimeout, None)]
    + [
        (kind, httpx.HTTPStatusError, int(kind.removeprefix("http_")))
        for kind in RenderFailure
        if kind.startswith("http_")
    ],
)
def test_assumed_errors_preserve_sdk_exception_details(
    failure: RenderFailure, cause: type[Exception], status: int | None
) -> None:
    with pytest.raises(RenderUnavailable) as caught:
        assumed_render_failure(failure, "No saved render", "candidate", RenderOptions(img_size=128))
    assert isinstance(caught.value.__cause__, cause)
    if status is not None:
        assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
        assert caught.value.__cause__.response.status_code == status
        assert caught.value.__cause__.request.url.params["img_size"] == "128"


def test_total_timeout_covers_all_recovery_trials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    calls = []
    log = Log(tmp_path)

    def execute(plan: dict[str, Any], remaining: float) -> dict[str, Any]:
        calls.append(remaining)
        Path(plan["output"]).mkdir(parents=True)
        now[0] = 31
        return {
            "passed": False,
            "missing_render_attempts": [{"request_key": "missing", "occurrence": 0, "failure": "connection"}],
        }

    try:
        result = recover_prompt(directory=tmp_path, plan={"prompt": "one"}, execute=execute, log=log, timeout=30)
    finally:
        log.close()
    assert calls == [30] and result["render_recovery"]["stopped_reason"] == "time_limit"

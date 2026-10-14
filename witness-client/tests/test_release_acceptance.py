"""The release gate fails closed, using only local fake evidence (no API calls)."""

import json
from pathlib import Path
from typing import Any

import pytest

import release_acceptance as check


@pytest.fixture
def evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state = tmp_path / "state-home/404-witness-client/exchange"
    state.mkdir(parents=True)
    session: dict[str, Any] = {
        "id": "exchange",
        "round": "test-0",
        "miner": "test-hotkey",
        "provider": "https://api.openai.com/v1/responses",
    }
    job = {"provider": "api"}
    request = json.dumps(
        {"model": "test-model", "stream": True, "input": "test"}
    ).encode()
    usage = {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}
    response = (
        b"data: "
        + json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": usage,
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "witness test OK"}
                            ],
                        }
                    ],
                },
            }
        ).encode()
        + b"\n\n"
    )
    manifest = {
        "prefix": "rounds/test-0/test-hotkey/exchange",
        "exchange_id": "exchange",
        "round": "test-0",
        "hotkey": "test-hotkey",
        "provider_rejected": False,
        "request_sha256": check.digest(request),
        "response_sha256": check.digest(response),
        "usage": usage,
    }
    status = {
        "status": "issued",
        "authorized": True,
        "provider_status": 200,
        "usage": usage,
        "usage_state": "recorded",
    }
    objects = {"request.json": request, "response.sse": response}
    for name, value in (("session.json", session), ("job.json", job)):
        check.save_json(state / name, value)
    (state / "request.json").write_bytes(request)
    (state / "response.bin").write_bytes(response)
    (tmp_path / "stdout.log").write_text(
        "witness test OK\nUsage: " + json.dumps(usage) + "\n"
    )

    def read(origin: str, prefix: str, destination: Path) -> dict[str, bytes]:
        return {
            **objects,
            "session.json": (state / "session.json").read_bytes(),
            "manifest.json": json.dumps(manifest).encode(),
            "status.json": json.dumps(status).encode(),
        }

    monkeypatch.setattr(check, "read_evidence", read)
    return {
        "directory": tmp_path,
        "state": state,
        "session": session,
        "manifest": manifest,
        "status": status,
        "objects": objects,
        "usage": usage,
    }


def verify(evidence: dict[str, Any], returncode: int = 0) -> dict[str, Any]:
    return check.verify_case(
        "check-api",
        evidence["directory"],
        "https://evidence.test",
        "test-0",
        "https://image.test/lorry.png",
        "test-model",
        returncode,
    )


def test_completed_public_output_and_usage(evidence: dict[str, Any]) -> None:
    assert verify(evidence)["status"] == "passed"


@pytest.mark.parametrize(
    "fault", ["usage", "request-hash", "provider", "stdout", "exit-code"]
)
def test_rejects_mismatched_public_results(
    evidence: dict[str, Any], fault: str
) -> None:
    if fault == "usage":
        evidence["manifest"]["usage"] = {
            "input_tokens": 30,
            "output_tokens": 5,
            "total_tokens": 35,
        }
    elif fault == "request-hash":
        evidence["manifest"]["request_sha256"] = "different"
    elif fault == "provider":
        evidence["session"][
            "provider"
        ] = "https://chatgpt.com/backend-api/codex/responses"
        check.save_json(evidence["state"] / "session.json", evidence["session"])
    elif fault == "stdout":
        (evidence["directory"] / "stdout.log").write_text(
            "witness test OK\nUsage: {}\n"
        )
    with pytest.raises(ValueError):
        verify(evidence, 1 if fault == "exit-code" else 0)


@pytest.mark.parametrize("streaming", [False, True])
def test_quota_rejection_is_blocked_and_preserves_diagnostics(
    evidence: dict[str, Any],
    streaming: bool,
) -> None:
    response = json.dumps(
        {
            "error": {
                "message": "Test quota exhausted",
                "code": "insufficient_quota",
                "param": None,
            }
        }
    ).encode()
    if streaming:
        response = (
            b"data: "
            + json.dumps(
                {
                    "type": "error",
                    "error": {
                        "code": "credit_balance_exhausted",
                        "message": "Test quota exhausted",
                        "param": None,
                    },
                }
            ).encode()
            + b"\n\n"
        )
    evidence["objects"]["response.sse"] = response
    evidence["manifest"].update(
        response_sha256=check.digest(response), usage=None, provider_rejected=True
    )
    evidence["status"].update(
        provider_status=200 if streaming else 429,
        usage=None,
        usage_state="unknown" if streaming else "rejected",
    )
    result = verify(evidence, 1)
    assert result["status"] == "blocked"
    assert result["usage"] is None
    assert result["diagnostics"]["provider_code"] == (
        "credit_balance_exhausted" if streaming else "insufficient_quota"
    )
    assert result["diagnostics"]["provider_message"] == "Test quota exhausted"
    with pytest.raises(ValueError, match="incorrectly accepted"):
        verify(evidence, 0)


@pytest.mark.parametrize(
    "status", ["failed", "blocked", "uncertain", "pending", "skipped"]
)
def test_each_required_case_must_pass(status: str) -> None:
    cases = {name: {"status": "passed"} for name in check.CASES}
    assert check.accepted(cases)
    cases["miner-api"]["status"] = status
    assert not check.accepted(cases)
    del cases["miner-api"]
    assert not check.accepted(cases)


@pytest.mark.parametrize("fault", [None, "javascript", "metadata"])
def test_miner_artifacts_must_match_evidence(
    evidence: dict[str, Any], fault: str | None
) -> None:
    directory = evidence["directory"] / "results"
    directory.mkdir()
    output = "const scene = new THREE.Scene();"
    response = evidence["objects"]["response.sse"].replace(
        b"witness test OK", output.encode()
    )
    evidence["objects"]["response.sse"] = response
    evidence["manifest"]["response_sha256"] = check.digest(response)
    (evidence["state"] / "response.bin").write_bytes(response)
    (directory / "001.js").write_text("different" if fault == "javascript" else output)
    check.save_json(
        directory / "001.json",
        {
            "hotkey": "test-hotkey",
            "image_url": "https://image.test/lorry.png",
            "result": "001.js",
            "usage": {} if fault == "metadata" else evidence["usage"],
        },
    )

    def run() -> dict[str, Any]:
        return check.verify_case(
            "miner-api",
            evidence["directory"],
            "https://evidence.test",
            "test-0",
            "https://image.test/lorry.png",
            "test-model",
            0,
        )

    if fault is None:
        assert run()["status"] == "passed"
    else:
        with pytest.raises(ValueError):
            run()


@pytest.mark.parametrize(
    "case", ["check-api", "check-subscription", "miner-api", "miner-subscription"]
)
@pytest.mark.parametrize("stream_format", ["item-events", "multiline-data"])
def test_valid_stream_formats_match_public_artifacts(
    evidence: dict[str, Any], case: str, stream_format: str
) -> None:
    subscription = case.endswith("subscription")
    miner = case.startswith("miner")
    output = "const scene = new THREE.Scene();" if miner else "witness test OK"
    if subscription:
        evidence["session"][
            "provider"
        ] = "https://chatgpt.com/backend-api/codex/responses"
        check.save_json(evidence["state"] / "session.json", evidence["session"])
        check.save_json(evidence["state"] / "job.json", {"provider": "codex"})
        request = json.loads(evidence["objects"]["request.json"])
        request.update(store=False, input=[{"role": "user", "content": "test"}])
        raw_request = json.dumps(request).encode()
        evidence["objects"]["request.json"] = raw_request
        evidence["manifest"]["request_sha256"] = check.digest(raw_request)
        (evidence["state"] / "request.json").write_bytes(raw_request)
    terminal = json.loads(
        evidence["objects"]["response.sse"].splitlines()[0].removeprefix(b"data: ")
    )
    item = terminal["response"]["output"][0]
    item["content"][0]["text"] = output
    if stream_format == "item-events":
        terminal["response"]["output"] = []
        events = [
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            terminal,
        ]
        response = b"".join(
            b"data: " + json.dumps(event).encode() + b"\n\n" for event in events
        )
    else:
        data = "\r\n".join(
            "data: " + line for line in json.dumps(terminal, indent=2).splitlines()
        )
        response = (
            "event: response.completed\r\n" + data + "\r\n\r\ndata: [DONE]\r\n\r\n"
        ).encode()
    evidence["objects"]["response.sse"] = response
    evidence["manifest"]["response_sha256"] = check.digest(response)
    (evidence["state"] / "response.bin").write_bytes(response)
    (evidence["directory"] / "stdout.log").write_text(
        output + "\nUsage: " + json.dumps(evidence["usage"]) + "\n"
    )
    if miner:
        directory = evidence["directory"] / "results"
        directory.mkdir()
        (directory / "001.js").write_text(output)
        check.save_json(
            directory / "001.json",
            {
                "hotkey": "test-hotkey",
                "image_url": "https://image.test/lorry.png",
                "result": "001.js",
                "usage": evidence["usage"],
            },
        )
    result = check.verify_case(
        case,
        evidence["directory"],
        "https://evidence.test",
        "test-0",
        "https://image.test/lorry.png",
        "test-model",
        0,
    )
    assert result["status"] == "passed"
    assert result["usage"] == evidence["usage"]
    assert result["output_bytes"] == len(output.encode())

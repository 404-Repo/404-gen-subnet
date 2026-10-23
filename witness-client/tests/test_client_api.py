"""Public generation and recovery preserve the same OpenAI result and errors."""

import json
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock
from typing import Literal, cast
from unittest.mock import MagicMock

import pytest
from bittensor_wallet import Keypair
from openai.types.responses import Response, ToolParam
from openai.types.responses.response_create_params import ResponseCreateParamsStreaming

from witness_client import WitnessClient, WitnessConfig, WitnessError
from witness_client.calls import Generation, Invocation
from witness_client.delivery import Phase
from witness_client.pods import CallView, Snapshot
from witness_client.tunnel import WebSocketTunnel
from witness_client.types import (
    CredentialProof,
    EarlyDisclosure,
    ExchangeStatus,
    HandshakeDisclosure,
    PrivateRecord,
    Session,
)


@pytest.fixture(autouse=True)
def isolated_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class ProverStub:
        verifying_key_sha256 = "a" * 64

        def __init__(self, parameters: Path) -> None:
            pass

    def unexpected_network(*args: object, **kwargs: object) -> None:
        pytest.fail("Client API tests must not contact a service")

    monkeypatch.setattr("witness_client.client.Prover", ProverStub)
    monkeypatch.setattr("httpx.Client.send", unexpected_network)


@pytest.fixture
def config(tmp_path: Path) -> WitnessConfig:
    return {
        "witness_url": "https://witness.test",
        "proof_parameters": str(tmp_path / "parameters"),
        "state_directory": str(tmp_path / "state"),
        "codex_home": str(tmp_path / "missing-codex-login"),
    }


@pytest.fixture
def client(config: WitnessConfig) -> Iterator[WitnessClient]:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    with WitnessClient(config, openai_api_key="x" * 80, hotkey=key) as value:
        yield value


def record_exchange(
    client: WitnessClient,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    status: int = 200,
) -> tuple[str, list[str]]:
    identifier = str(uuid.uuid4())
    calls: list[str] = []
    session: Session = {
        "id": identifier,
        "protocol": 2,
        "provider": (
            "https://chatgpt.com/backend-api/codex/responses"
            if client._mode == "codex"
            else "https://api.openai.com/v1/responses"
        ),
        "proof_key_sha256": client._prover.verifying_key_sha256,
        "round": "1",
        "miner": client._hotkey.ss58_address,
    }

    def enqueue(provider: Literal["api", "codex"]) -> Session:
        assert provider == client._mode
        calls.append("enqueue")
        directory = client._state / identifier
        directory.mkdir(mode=0o700)
        (directory / "claim.json").write_text("{}")
        return session

    @contextmanager
    def tunnel(exchange_id: str) -> Iterator[WebSocketTunnel]:
        assert exchange_id == identifier
        yield WebSocketTunnel(MagicMock(), identifier)

    def exchange(*args: object, **kwargs: object) -> bytes:
        calls.append("generate")
        raw = (
            f"HTTP/1.1 {status} Provider\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        return raw

    def download(exchange_id: str) -> bytes:
        assert exchange_id == identifier
        calls.append("download")
        return body

    monkeypatch.setattr(client._gateway, "queue_and_wait_for_worker", enqueue)
    monkeypatch.setattr(client._gateway, "tunnel", tunnel)
    monkeypatch.setattr(client._gateway, "remaining_seconds", lambda _: 60)
    monkeypatch.setattr(
        client._gateway,
        "wait_for_evidence",
        lambda _: {
            "status": "issued",
            "provider_status": status,
            "evidence_expires": 9_999_999_999_999,
        },
    )
    monkeypatch.setattr(client._gateway, "response", download)
    monkeypatch.setattr("witness_client.client.exchange", exchange)
    return identifier, calls


def event(response: dict[str, object]) -> bytes:
    return (
        b"data: "
        + json.dumps(
            {"type": f"response.{response['status']}", "response": response}
        ).encode()
        + b"\n\n"
    )


@pytest.mark.parametrize("preparations, call_limit", [(1, 4), (3, 4), (3, 2)])
def test_pod_calls_bound_preparation_and_capture_concurrency(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, preparations: int, call_limit: int
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    config = {**config, "transport": "pods-v1", "round": "test-0"}
    reserved, preparation_full, inference_full, excess, allow_authorization, allow_completion = (
        Event() for _ in range(6)
    )
    lock = Lock()
    preparing = generating = active = maximum_preparing = maximum_generating = maximum_active = 0
    calls: dict[str, Snapshot] = {}

    def session(identifier: str) -> Session:
        return {
            "id": identifier,
            "protocol": 2,
            "provider": "https://api.openai.com/v1/responses",
            "proof_key_sha256": "a" * 64,
            "round": "test-0",
            "miner": key.ss58_address,
        }

    def missing(_: Generation) -> None:
        raise WitnessError("No recording", status_code=404)

    def reserve(invocation: Invocation) -> Snapshot:
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, invocation.generation.id))
        calls[identifier] = Snapshot(
            call=CallView(
                request=invocation.request,
                exchange_id=identifier,
                owner_ordinal=0,
                deadline=int(time.time()) + 60,
                phase="reserved",
            ),
            round="test-0",
            hotkey=key.ss58_address,
            session=None,
        )
        if len(calls) == 4:
            reserved.set()
        return calls[identifier]

    def dispatch(invocation: Invocation, _: str) -> Snapshot:
        nonlocal preparing, active, maximum_preparing, maximum_active
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, invocation.generation.id))
        with lock:
            preparing += 1
            active += 1
            maximum_preparing = max(maximum_preparing, preparing)
            maximum_active = max(maximum_active, active)
            if preparing > preparations or active > call_limit:
                excess.set()
            if preparing == min(preparations, call_limit):
                preparation_full.set()
        current = calls[identifier]
        current.call.phase = "capturing"
        current.session = dict(session(identifier))
        return current

    @contextmanager
    def tunnel(identifier: str) -> Iterator[WebSocketTunnel]:
        yield WebSocketTunnel(MagicMock(), identifier)

    def authorize(
        _: WitnessClient, tunnel: WebSocketTunnel, disclosure: EarlyDisclosure
    ) -> None:
        nonlocal preparing, generating, maximum_generating
        with lock:
            preparing -= 1
            generating += 1
            maximum_generating = max(maximum_generating, generating)
            if generating == call_limit:
                inference_full.set()

    def exchange(tunnel: WebSocketTunnel, *args: object, **kwargs: object) -> bytes:
        nonlocal generating, active
        assert allow_authorization.wait(3)
        cast(Callable[[EarlyDisclosure], None], kwargs["authorize"])(
            cast(EarlyDisclosure, {})
        )
        assert allow_completion.wait(3)
        calls[tunnel.identifier].call.phase = "issued"
        with lock:
            generating -= 1
            active -= 1
        return b""

    def recorded(
        invocation: Invocation, call: CallView, proof_hash: str
    ) -> tuple[Session, int, bytes]:
        return (
            session(call.exchange_id),
            200,
            event(
                {"id": "resp_" + call.exchange_id, "status": "completed", "output": []}
            ),
        )

    with WitnessClient(
        config, hotkey=key, openai_api_key="x" * 80, parallel_preparations=preparations
    ) as witness:
        gateway = witness._gateway
        for name, value in {
            "recording": missing,
            "reserve": reserve,
            "dispatch": dispatch,
            "ensure_pod": lambda: str(uuid.uuid4()),
            "tunnel": tunnel,
            "snapshot": lambda identifier: calls[identifier],
            "recorded": recorded,
            "delivered": lambda invocation: 0,
        }.items():
            monkeypatch.setattr(gateway, name, value, raising=False)
        monkeypatch.setattr(WitnessClient, "_authorize", authorize)
        monkeypatch.setattr("witness_client.client.exchange", exchange)
        slots = BoundedSemaphore(call_limit)
        bound = [
            witness.for_generation(bytes([index]), 42, call_slots=slots)
            for index in range(4)
        ]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(generation.create, stream=True, model="fixture", input="image")
                for generation in bound
            ]
            try:
                assert reserved.wait(3)
                assert preparation_full.wait(3)
                assert not excess.wait(0.05)
                with lock:
                    assert preparing == min(preparations, call_limit)
                allow_authorization.set()
                assert inference_full.wait(3)
                assert not excess.wait(0.05)
                with lock:
                    assert generating == call_limit
            finally:
                allow_authorization.set()
                allow_completion.set()
            assert len({future.result(timeout=5).response.id for future in futures}) == 4
    assert maximum_preparing == min(preparations, call_limit)
    assert maximum_generating == maximum_active == call_limit


@pytest.mark.parametrize("round_remaining", [10.0, 1000.0])
def test_create_verifies_on_active_tunnel_after_saving_proof(
    client: WitnessClient, monkeypatch: pytest.MonkeyPatch, round_remaining: float
) -> None:
    body = event({"id": "resp_complete", "status": "completed", "output": []})
    identifier, _ = record_exchange(client, monkeypatch, body)
    active: WebSocketTunnel | None = None
    remaining = [round_remaining]
    steps: list[str] = []
    disclosure: HandshakeDisclosure = {
        "disclosures": {"client": [], "server": []},
        "response_key": {"key": "00" * 32, "iv": "00" * 12, "number": 0},
        "client_bytes": 0,
        "probe": True,
    }

    def prove(records: list[PrivateRecord]) -> CredentialProof:
        steps.append("prove")
        remaining[0] = 3.0
        return {
            "nonce": "00" * 12,
            "bundle": {"commitment": "fixture", "mac_keys": [], "proofs": []},
        }

    def verify(
        tunnel: WebSocketTunnel, phase: Phase, claim: bytes, timeout: float
    ) -> ExchangeStatus:
        assert tunnel is active
        assert tunnel.identifier == identifier
        steps.append(phase)
        if phase == "handshake":
            assert timeout == min(client._gateway.delivery_timeout, round_remaining)
            assert "credential" not in json.loads(claim)
        else:
            assert timeout == 3.0, "Proof preparation must consume the remaining time"
            assert (client._state / identifier / "claim.json").read_bytes() == claim
            assert "credential" in json.loads(claim)
        return {
            "status": "running",
            "handshook": True,
            "authorized": phase == "authorize",
        }

    def exchange(
        tunnel: WebSocketTunnel,
        host: str,
        chunks: Sequence[tuple[bool, bytes]],
        *,
        handshake: Callable[[HandshakeDisclosure], None],
        authorize: Callable[[EarlyDisclosure], None],
        timeout: float,
    ) -> bytes:
        nonlocal active
        active = tunnel
        assert timeout == round_remaining
        handshake(disclosure)
        authorize({**disclosure, "private": []})
        return f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body

    monkeypatch.setattr(client._prover, "prove", prove, raising=False)
    monkeypatch.setattr(client._gateway, "remaining_seconds", lambda _: remaining[0])
    monkeypatch.setattr(WebSocketTunnel, "verify", verify)
    monkeypatch.setattr("witness_client.client.exchange", exchange)
    assert (
        client.create(stream=True, model="fixture", input="hello").response.id
        == "resp_complete"
    )
    assert steps == ["handshake", "prove", "authorize"]


@pytest.mark.parametrize(
    "params",
    [{"stream": None}, {"stream": False}, {"stream": 1}, {"stream": "true"}],
)
def test_create_rejects_invalid_stream_before_queueing(
    client: WitnessClient,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, object],
) -> None:
    def unexpected_queueing(provider: Literal["api", "codex"]) -> None:
        pytest.fail("Invalid streaming options must not allocate a worker")

    monkeypatch.setattr(
        client._gateway, "queue_and_wait_for_worker", unexpected_queueing
    )
    with pytest.raises(ValueError, match="requires streaming"):
        client.create(
            **cast(
                ResponseCreateParamsStreaming,
                {"model": "fixture", "input": "hello", **params},
            )
        )


@pytest.mark.parametrize("invalid", ["credential", "body"])
def test_local_validation_fails_before_queueing(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())

    def unexpected_queueing(provider: Literal["api", "codex"]) -> None:
        pytest.fail("Invalid local requests must not allocate a worker")

    api_key = "too-short" if invalid == "credential" else "x" * 80
    prompt = "test" if invalid == "credential" else "x" * (8 * 1024 * 1024)
    message = "credential format" if invalid == "credential" else "exceeds 8 MiB"
    with WitnessClient(config, openai_api_key=api_key, hotkey=key) as client:
        monkeypatch.setattr(
            client._gateway, "queue_and_wait_for_worker", unexpected_queueing
        )
        with pytest.raises(ValueError, match=message):
            client.create(stream=True, model="fixture", input=prompt)


def test_api_and_subscription_use_the_same_gateway_configuration(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    monkeypatch.setattr(
        "witness_client.request.subscription_credentials",
        lambda *args: ("x" * 80, "abc-123"),
    )
    body = event({"id": "resp_complete", "status": "completed", "output": []})
    for mode in ("api", "codex"):
        with WitnessClient(
            config,
            openai_api_key="x" * 80 if mode == "api" else None,
            subscription="codex" if mode == "codex" else None,
            hotkey=key,
        ) as client:
            assert str(client._http.base_url).rstrip("/") == config[
                "witness_url"
            ].rstrip("/")
            _, calls = record_exchange(client, monkeypatch, body)
            result = client.create(stream=True, model="fixture", input="hello")
            assert result.response.id == "resp_complete"
            assert calls == ["enqueue", "generate"]


@pytest.mark.parametrize("mode", ["api", "codex"])
@pytest.mark.parametrize("indices", [(0, 1), (1, 2), (0, 2), (2, 5)])
def test_streamed_output_survives_create_and_downloaded_recovery(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["api", "codex"],
    indices: tuple[int, int],
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    monkeypatch.setattr(
        "witness_client.request.subscription_credentials",
        lambda *args: ("x" * 80, "abc-123"),
    )
    text = "const scene = new THREE.Scene();"
    output: list[dict[str, object]] = [
        {
            "id": "rs_1",
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "retained-reasoning",
        },
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        },
    ]
    events: list[dict[str, object]] = [
        {"type": "response.output_item.added", "output_index": index, "item": None}
        for index in range(indices[-1] + 1)
        if index not in indices
    ]
    events += [
        {
            "type": "response.output_item.done",
            "output_index": indices[0],
            "item": output[0],
        },
        {
            "type": "response.output_text.delta",
            "output_index": indices[1],
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "output_index": indices[1],
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.output_item.done",
            "output_index": indices[1],
            "item": output[1],
        },
    ]
    body = b"".join(
        b"data: " + json.dumps(value).encode() + b"\n\n" for value in events
    ) + event(
        {
            "id": "resp_complete",
            "status": "completed",
            "output": [],
            "usage": {"input_tokens": 4, "output_tokens": 12, "total_tokens": 16},
        }
    )
    with WitnessClient(
        config,
        openai_api_key="x" * 80 if mode == "api" else None,
        subscription="codex" if mode == "codex" else None,
        hotkey=key,
    ) as client:
        identifier, calls = record_exchange(client, monkeypatch, body)
        created = client.responses.create(model="fixture", input="hello")
        (client._state / identifier / "response.bin").unlink()
        recovered = client.resume(identifier)

    for result in (created, recovered):
        assert result.output_text == result.response.output_text == text
        assert [
            item.model_dump(exclude_unset=True) for item in result.response.output
        ] == output
        assert result.usage == {
            "input_tokens": 4,
            "output_tokens": 12,
            "total_tokens": 16,
        }
    assert recovered == created
    assert calls == ["enqueue", "generate", "download"]


@pytest.mark.parametrize("status", ["incomplete", "failed"])
def test_terminal_provider_failure_matches_on_create_and_resume(
    client: WitnessClient, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    body = event(
        {
            "id": "resp_terminal",
            "status": status,
            "output": (
                [
                    {
                        "id": "msg_partial",
                        "type": "message",
                        "role": "assistant",
                        "status": "incomplete",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "const scene =",
                                "annotations": [],
                            }
                        ],
                    }
                ]
                if status == "incomplete"
                else []
            ),
            "incomplete_details": (
                {"reason": "max_output_tokens"} if status == "incomplete" else None
            ),
            "error": (
                {"code": "server_error", "message": "Generation failed"}
                if status == "failed"
                else None
            ),
            "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        }
    )
    identifier, calls = record_exchange(client, monkeypatch, body)
    with pytest.raises(WitnessError) as created:
        client.create(stream=True, model="fixture", input="hello")
    with pytest.raises(WitnessError) as recovered:
        client.resume(identifier)

    for error in (created.value, recovered.value):
        assert error.exchange_id == identifier
        assert error.source == "provider"
        assert error.code == f"provider_{status}"
        assert isinstance(error.response, Response)
        assert error.response.id == "resp_terminal"
        assert error.response.status == status
        assert error.response.usage is not None
        assert error.response.usage.total_tokens == 6
        if status == "failed":
            assert error.provider_code == "server_error"
            assert error.provider_message == "Generation failed"
        else:
            assert error.response.incomplete_details is not None
            assert error.response.incomplete_details.reason == "max_output_tokens"
            assert error.response.output_text == "const scene ="
    assert calls == ["enqueue", "generate"]


def test_missing_subscription_login_fails_before_queueing(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())

    def unexpected_queueing(provider: Literal["api", "codex"]) -> None:
        pytest.fail("Missing credentials must not allocate a worker")

    with WitnessClient(config, subscription="codex", hotkey=key) as client:
        monkeypatch.setattr(
            client._gateway, "queue_and_wait_for_worker", unexpected_queueing
        )
        with pytest.raises(WitnessError) as failure:
            client.create(stream=True, model="fixture", input="hello")
    assert failure.value.code == "credentials_unavailable"
    assert failure.value.source == "client"
    assert failure.value.exchange_id is None
    assert isinstance(failure.value.__cause__, FileNotFoundError)
    assert str(failure.value.__cause__) in str(failure.value)
    assert "auth.json" in str(failure.value)


def test_unreadable_subscription_login_preserves_reason_before_queueing(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    original = PermissionError("Permission denied reading Codex auth.json")

    def credentials(*args: object) -> tuple[str, str]:
        raise original

    def unexpected_queueing(provider: Literal["api", "codex"]) -> None:
        pytest.fail("Unreadable credentials must not allocate a worker")

    monkeypatch.setattr("witness_client.request.subscription_credentials", credentials)
    with WitnessClient(config, subscription="codex", hotkey=key) as client:
        monkeypatch.setattr(
            client._gateway, "queue_and_wait_for_worker", unexpected_queueing
        )
        with pytest.raises(WitnessError) as failure:
            client.create(stream=True, model="fixture", input="hello")

    assert failure.value.code == "credentials_unavailable"
    assert failure.value.source == "client"
    assert failure.value.exchange_id is None
    assert failure.value.__cause__ is original
    assert str(original) in str(failure.value)


def test_request_iterator_failure_is_not_reported_as_credentials_failure(
    client: WitnessClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RuntimeError("Tool definitions could not be loaded")

    def tools() -> Iterator[ToolParam]:
        yield from ()
        raise original

    def unexpected_queueing(provider: Literal["api", "codex"]) -> None:
        pytest.fail("An invalid request must not allocate a worker")

    monkeypatch.setattr(
        client._gateway, "queue_and_wait_for_worker", unexpected_queueing
    )
    with pytest.raises(RuntimeError) as failure:
        client.create(stream=True, model="fixture", input="hello", tools=tools())

    assert failure.value is original


def test_subscription_refresh_failure_closes_unused_worker_and_retains_error(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    original = RuntimeError("Local Codex login refresh did not finish")
    credentials_calls = 0

    def credentials(*args: object) -> tuple[str, str]:
        nonlocal credentials_calls
        credentials_calls += 1
        if credentials_calls == 2:
            raise original
        return "x" * 80, "abc-123"

    monkeypatch.setattr("witness_client.request.subscription_credentials", credentials)
    with WitnessClient(config, subscription="codex", hotkey=key) as client:
        identifier, calls = record_exchange(client, monkeypatch, b"")

        def close_worker(exchange_id: str) -> None:
            assert exchange_id == identifier
            calls.append("close")

        monkeypatch.setattr(client._gateway, "release", close_worker)
        with pytest.raises(WitnessError) as failure:
            client.create(stream=True, model="fixture", input="hello")

    assert failure.value.code == "credentials_unavailable"
    assert failure.value.source == "client"
    assert failure.value.exchange_id == identifier
    assert failure.value.__cause__ is original
    assert str(original) in str(failure.value)
    assert credentials_calls == 2
    assert calls == ["enqueue", "close"]


def test_provider_http_error_details_match_on_create_and_resume(
    client: WitnessClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps(
        {
            "error": {
                "code": "unsupported_value",
                "param": "temperature",
                "message": "This model does not support temperature=0.3.",
            }
        }
    ).encode()
    identifier, calls = record_exchange(client, monkeypatch, body, status=400)
    with pytest.raises(WitnessError) as created:
        client.create(stream=True, model="fixture", input="hello", temperature=0.3)
    with pytest.raises(WitnessError) as recovered:
        client.resume(identifier)

    for error in (created.value, recovered.value):
        assert error.exchange_id == identifier
        assert error.source == "provider"
        assert error.status_code == 400
        assert error.code == "provider_rejected"
        assert error.provider_code == "unsupported_value"
        assert error.param == "temperature"
        assert error.provider_message == "This model does not support temperature=0.3."
    assert calls == ["enqueue", "generate", "download"]


@pytest.mark.parametrize("output_kind", ["function_call", "refusal"])
@pytest.mark.parametrize("streamed", [False, True])
def test_non_text_results_preserve_typed_output_and_reasoning(
    client: WitnessClient,
    monkeypatch: pytest.MonkeyPatch,
    output_kind: str,
    streamed: bool,
) -> None:
    output = (
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "render",
            "arguments": '{"scene":"box"}',
        }
        if output_kind == "function_call"
        else {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "refusal", "refusal": "I cannot process this request."}
            ],
        }
    )
    output_items = [
        {
            "id": "rs_1",
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "retained-reasoning",
        },
        output,
    ]
    body = event(
        {
            "id": "resp_complete",
            "status": "completed",
            "output": [] if streamed else output_items,
            "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        }
    )
    if streamed:
        body = (
            b"".join(
                b"data: "
                + json.dumps(
                    {
                        "type": "response.output_item.done",
                        "output_index": i,
                        "item": item,
                    }
                ).encode()
                + b"\n\n"
                for i, item in enumerate(output_items)
            )
            + body
        )
    identifier, calls = record_exchange(client, monkeypatch, body)
    result = client.create(stream=True, model="fixture", input="hello")
    assert result.output_text == ""
    assert result.usage == {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
    assert isinstance(result.response, Response)
    assert result.response.id == "resp_complete"
    reasoning = result.response.output[0]
    assert reasoning.type == "reasoning"
    assert reasoning.encrypted_content == "retained-reasoning"
    item = result.response.output[1]
    if output_kind == "function_call":
        assert item.type == "function_call"
        assert item.name == "render"
        assert item.call_id == "call_1"
        assert item.arguments == '{"scene":"box"}'
    else:
        assert item.type == "message"
        assert item.content[0].type == "refusal"
        assert item.content[0].refusal == "I cannot process this request."
    assert client.resume(identifier) == result
    assert calls == ["enqueue", "generate"]


def test_cache_write_usage_is_typed_and_preserved_on_recovery(
    client: WitnessClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = event(
        {
            "id": "resp_cache_write",
            "status": "completed",
            "output": [],
            "usage": {
                "input_tokens": 12,
                "output_tokens": 0,
                "total_tokens": 12,
                "input_tokens_details": {
                    "cached_tokens": 4,
                    "cache_write_tokens": 8,
                },
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
    )
    identifier, calls = record_exchange(client, monkeypatch, body)
    created = client.create(stream=True, model="fixture", input="hello")
    for result in (created, client.resume(identifier)):
        cache_writes: int = result.usage["input_tokens_details"]["cache_write_tokens"]
        assert cache_writes == 8
        assert result.response.usage is not None
        assert (
            result.response.usage.input_tokens_details.cache_write_tokens
            == cache_writes
        )
        assert result.usage["input_tokens_details"]["cached_tokens"] == 4
        assert result.usage["input_tokens"] == 12
    assert calls == ["enqueue", "generate"]

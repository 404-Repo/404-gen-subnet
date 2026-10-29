"""Direct calls respect preparation and capture concurrency."""

import json
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import MagicMock

import openai
import pytest
from bittensor_wallet import Keypair
from witness_client import WitnessClient, WitnessConfig, WitnessError
from witness_client.calls import RequestKey
from witness_client.client import CapturedResponse
from witness_client.pods import CallView, Snapshot
from witness_client.response import ModelStream
from witness_client.tunnel import WebSocketTunnel
from witness_client.types import (
    CredentialProof,
    EarlyDisclosure,
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


def event(response: dict[str, object]) -> bytes:
    return (
        b"data: "
        + json.dumps(
            {"type": f"response.{response['status']}", "response": response}
        ).encode()
        + b"\n\n"
    )


@pytest.mark.parametrize(
    "parts, expected",
    [
        ([(0, 0, "original")], "original"),
        ([(1, 0, "second"), (0, 1, " part"), (0, 0, "first")], "first partsecond"),
    ],
)
def test_responses_create_preserves_text_missing_from_terminal_output(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
    parts: list[tuple[int, int, str]],
    expected: str,
) -> None:
    body = b"".join(
        b"data: " + json.dumps({
            "type": "response.output_text.done",
            "output_index": output_index,
            "content_index": content_index,
            "text": text,
        }).encode() + b"\n\n"
        for output_index, content_index, text in parts
    ) + event({
        "id": "resp_text",
        "status": "completed",
        "output": [],
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    })

    def resolve(
        _: WitnessClient, key: RequestKey, request: bytes, session_id: str | None
    ) -> CapturedResponse:
        return CapturedResponse(str(uuid.uuid4()), 200, body)

    monkeypatch.setattr(WitnessClient, "_resolve", resolve)
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    with WitnessClient(config, hotkey=key, openai_api_key="x" * 80) as witness:
        response = witness.responses.create(model="fixture", input="image")

    assert response.output_text == expected
    assert response.id == "resp_text"
    assert response.usage is not None
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 2
    assert response.usage.total_tokens == 5


@pytest.mark.parametrize("preparations, call_limit", [(1, 4), (3, 4), (3, 2)])
@pytest.mark.parametrize("legacy_slots", [False, True])
def test_pod_calls_bound_preparation_and_capture_concurrency(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, preparations: int, call_limit: int, legacy_slots: bool,
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
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

    def start(invocation: RequestKey) -> Snapshot:
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, invocation.id))
        calls[identifier] = Snapshot(
            call=CallView(
                request=invocation,
                exchange_id=identifier,
                deadline=int(time.time()) + 60,
                phase="assigned",
            ),
            hotkey=key.ss58_address,
            session=dict(session(identifier)),
        )
        if len(calls) == 4:
            reserved.set()
        return calls[identifier]

    @contextmanager
    def tunnel(identifier: str, *, deadline: float) -> Iterator[WebSocketTunnel]:
        nonlocal preparing, active, maximum_preparing, maximum_active
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
        yield WebSocketTunnel(MagicMock(), identifier)

    def authorize(
        _: WitnessClient, tunnel: WebSocketTunnel, disclosure: EarlyDisclosure, *, deadline: float,
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
        invocation: RequestKey, call: CallView, proof_hash: str, **kwargs: object
    ) -> tuple[Session, int, bytes]:
        return (
            session(call.exchange_id),
            200,
            event(
                {"id": "resp_" + call.exchange_id, "status": "completed", "output": []}
            ),
        )

    with WitnessClient(
        config, hotkey=key, openai_api_key="x" * 80, parallel_calls=call_limit, parallel_preparations=preparations,
        call_slots=BoundedSemaphore(call_limit) if legacy_slots else None
    ) as witness:
        gateway = witness._gateway
        for name, value in {
            "start": start,
            "tunnel": tunnel,
            "snapshot": lambda identifier, **kwargs: calls[identifier],
            "recorded": recorded,
        }.items():
            monkeypatch.setattr(gateway, name, value, raising=False)
        monkeypatch.setattr(WitnessClient, "_authorize", authorize)
        monkeypatch.setattr("witness_client.client.exchange", exchange)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(witness.create, model="fixture", input="image", request_id=i) for i in range(4)]
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
            assert len({future.result(timeout=5).id for future in futures}) == 4
    assert maximum_preparing == min(preparations, call_limit)
    assert maximum_generating == maximum_active == call_limit


@pytest.mark.parametrize("authorized, not_dispatched", [(False, False), (True, False), (True, True)])
def test_pod_failed_capture_allows_same_id_retry(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, authorized: bool, not_dispatched: bool,
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = str(uuid.uuid4())
    clock = [1000.0]
    snapshots: list[str] = []
    current: Snapshot | None = None

    def start(
        invocation: RequestKey, *, expected_exchange_id: str | None = None, deadline: float | None = None,
    ) -> Snapshot:
        nonlocal current
        if current is not None:
            if expected_exchange_id == identifier:
                current = current.model_copy(deep=True)
                current.call.exchange_id = str(uuid.uuid4())
                current.call.phase = "issued"
            return current
        current = Snapshot(
            call=CallView(
                request=invocation, exchange_id=identifier,
                deadline=1002, phase="capturing", authorized=authorized, not_dispatched=not_dispatched,
            ),
            hotkey=key.ss58_address, session=None,
        )
        return current

    def snapshot(exchange_id: str, **kwargs: object) -> Snapshot:
        snapshots.append(exchange_id)
        assert current is not None
        if current.call.exchange_id == identifier:
            current.call.phase = "failed"
        return current

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def repeat(*args: object, **kwargs: object) -> None:
        pytest.fail("Unresolved capture must not request another exchange or inference")

    monkeypatch.setattr("witness_client.client.time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: 1000.0, sleep=sleep,
    ))
    with WitnessClient(config, hotkey=key, openai_api_key="x" * 80, delivery_timeout=1) as witness:
        for name, value in {
            "start": start, "snapshot": snapshot,
            "ensure_pod": repeat, "tunnel": repeat,
        }.items():
            monkeypatch.setattr(witness._gateway, name, value)
        bound = witness
        with pytest.raises(openai.APIConnectionError) as connection:
            bound.create(stream=True, model="fixture", input="image")
        assert isinstance(connection.value.__cause__, WitnessError)
        assert connection.value.__cause__.code == "capture_failed"
        with pytest.raises(WitnessError) as error:
            witness.resume(identifier)
        assert error.value.code == "capture_failed"
        assert error.value.exchange_id == identifier and error.value.source == "gateway"
        monkeypatch.setattr(witness._gateway, "recorded", lambda *args, **kwargs: (
            cast(Session, {}), 200, event({"id": "retry", "status": "completed", "output": []}),
        ))
        assert witness.create(model="fixture", input="image").id == "retry"
        assert witness.create(model="fixture", input="image").id == "retry"


@pytest.mark.parametrize("recovered", [False, True])
def test_expired_undispatched_failure_uses_a_bounded_recovery_window(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, recovered: bool,
) -> None:
    key = Keypair.create_from_uri("//Alice")
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    clock = [1000.0]
    starts: list[str | None] = []

    def start(
        invocation: RequestKey, *, expected_exchange_id: str | None = None,
        deadline: float | None = None,
    ) -> Snapshot:
        starts.append(expected_exchange_id)
        ready = recovered and expected_exchange_id is not None
        return Snapshot(
            call=CallView(
                request=invocation, exchange_id=second if ready else first,
                deadline=2000 if ready else 900, phase="issued" if ready else "failed",
                not_dispatched=not ready,
            ),
            hotkey=key.ss58_address, session=None,
        )

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def recorded(*args: object, **kwargs: object) -> tuple[Session, int, bytes]:
        return cast(Session, {}), 200, event({
            "id": "resp_recovered", "status": "completed", "output": [],
        })

    monkeypatch.setattr("witness_client.client.time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[0], sleep=sleep,
    ))
    with WitnessClient(config, hotkey=key, openai_api_key="x" * 80, delivery_timeout=5) as witness:
        monkeypatch.setattr(witness._gateway, "start", start)
        monkeypatch.setattr(witness._gateway, "recorded", recorded)
        if recovered:
            response = witness.responses.create(model="fixture", input="image")
            assert response.id == "resp_recovered" and starts == [None, first]
        else:
            with pytest.raises(openai.APIConnectionError) as error:
                witness.responses.create(model="fixture", input="image")
            assert isinstance(error.value.__cause__, WitnessError)
            assert error.value.__cause__.code == "capture_failed"
            assert starts == [None, first, first]
        assert clock[0] < 1005


@pytest.mark.parametrize("stage", ["handshake", "proof", "flush"])
@pytest.mark.parametrize("elapsed", [2, 6])
def test_capture_deadline_bounds_verification_and_prevents_late_authorization(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, stage: str, elapsed: int,
) -> None:
    key = Keypair.create_from_uri("//Alice")
    identifier = str(uuid.uuid4())
    clock = [1000.0]
    sent: list[str] = []
    waits: list[float | None] = []
    current: Snapshot | None = None

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    timer = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0], sleep=sleep)
    monkeypatch.setattr("witness_client.client.time", timer)
    monkeypatch.setattr("witness_client.tunnel.time", timer)

    def start(invocation: RequestKey) -> Snapshot:
        nonlocal current
        current = Snapshot(
            call=CallView(request=invocation, exchange_id=identifier, deadline=2800, phase="assigned"),
            hotkey=key.ss58_address, session={
                "id": identifier, "protocol": 2, "provider": "https://api.openai.com/v1/responses",
                "proof_key_sha256": "a" * 64, "round": "test-0", "miner": key.ss58_address,
            },
        )
        return current

    class Socket:
        def send(self, message: str | bytes) -> None:
            if isinstance(message, bytes):
                if stage == "flush":
                    clock[0] += elapsed
                return
            sent.append(str(json.loads(message)["type"]))

        def recv(self, timeout: float | None = None) -> str:
            waits.append(timeout)
            assert current is not None
            current.call.phase = "capturing"
            current.call.authorized = sent[-1] == "authorize"
            return json.dumps({"type": "status", "status": "running",
                               "handshook": True, "authorized": current.call.authorized})

        def close(self) -> None:
            pass

    @contextmanager
    def tunnel(exchange_id: str, **kwargs: object) -> Iterator[WebSocketTunnel]:
        yield WebSocketTunnel(Socket(), exchange_id)

    def prove(private: list[PrivateRecord]) -> CredentialProof:
        if stage == "proof":
            clock[0] += elapsed
        return cast(CredentialProof, {})

    def snapshot(exchange_id: str, **kwargs: object) -> Snapshot:
        assert current is not None
        return current

    def exchange(tunnel: WebSocketTunnel, *args: object, **kwargs: object) -> bytes:
        disclosure = cast(EarlyDisclosure, {
            "private": [], "disclosures": {"client": [], "server": []},
            "response_key": {}, "client_bytes": 1, "probe": True,
        })
        if stage == "flush":
            tunnel.send(b"encrypted request")
        if stage == "handshake":
            clock[0] += elapsed
            cast(Callable[[HandshakeDisclosure], None], kwargs["handshake"])(disclosure)
        cast(Callable[[EarlyDisclosure], None], kwargs["authorize"])(disclosure)
        raise TimeoutError("Fixture response remains unresolved")

    monkeypatch.setattr("witness_client.client.exchange", exchange)
    with WitnessClient(config, hotkey=key, openai_api_key="x" * 80, delivery_timeout=5) as witness:
        monkeypatch.setattr(witness._prover, "prove", prove, raising=False)
        for name, value in {"start": start, "tunnel": tunnel, "snapshot": snapshot}.items():
            monkeypatch.setattr(witness._gateway, name, value)
        with pytest.raises(openai.APITimeoutError):
            witness.responses.create(model="fixture", input="image")
    if elapsed > 5:
        assert sent == [] and waits == []
    else:
        assert sent == (["handshake", "authorize"] if stage == "handshake" else ["authorize"])
        assert waits == [3] * len(sent)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("ready", [False, True])
def test_public_create_waits_for_the_existing_capture_or_times_out(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, stream: Literal[False, True], ready: bool,
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = str(uuid.uuid4())
    clock = [1000.0]
    polls: list[float] = []
    current: Snapshot | None = None

    def start(invocation: RequestKey) -> Snapshot:
        nonlocal current
        current = Snapshot(
            call=CallView(request=invocation, exchange_id=identifier, deadline=4600,
                          phase="capturing", authorized=True),
            hotkey=key.ss58_address, session=None,
        )
        return current

    def snapshot(exchange_id: str, *, deadline: float) -> Snapshot:
        assert exchange_id == identifier and deadline == 1001
        assert current is not None
        polls.append(clock[0])
        if ready and clock[0] >= 1000.5:
            current.call.phase = "issued"
        return current

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def repeat(*args: object, **kwargs: object) -> None:
        pytest.fail("An existing paid capture must only be polled")

    def recorded(*args: object, **kwargs: object) -> tuple[Session, int, bytes]:
        return cast(Session, {}), 200, event({
            "id": "resp_saved", "status": "completed", "output": [],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        })

    monkeypatch.setattr("witness_client.client.time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: 1000.0, sleep=sleep,
    ))
    with WitnessClient(config, hotkey=key, openai_api_key="x" * 80, delivery_timeout=1) as witness:
        for name, value in {
            "start": start, "snapshot": snapshot, "tunnel": repeat, "recorded": recorded,
        }.items():
            monkeypatch.setattr(witness._gateway, name, value)
        if ready:
            result = witness.responses.create(stream=stream, model="fixture", input="image")
            if stream:
                assert isinstance(result, ModelStream)
                terminal = list(result)[-1]
                assert terminal.type == "response.completed"
                assert terminal.response.id == "resp_saved"
                assert terminal.response.usage is not None
                assert terminal.response.usage.total_tokens == 5
            else:
                assert not isinstance(result, ModelStream)
                assert result.id == "resp_saved"
            assert polls == [1000, 1000.5]
        else:
            with pytest.raises(openai.APITimeoutError) as error:
                witness.responses.create(stream=stream, model="fixture", input="image")
            cause = error.value.__cause__
            assert isinstance(cause, WitnessError)
            assert cause.code == "capture_timeout"
            assert cause.exchange_id == identifier
            assert clock[0] == 1001 and polls == [1000, 1000.5]


@pytest.mark.parametrize("mode", ["api", "codex"])
def test_invalid_session_id_fails_before_credentials_or_allocation(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["api", "codex"],
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())

    def unexpected_credentials(*args: object) -> None:
        pytest.fail("Invalid session_id must fail before loading credentials")

    monkeypatch.setattr("witness_client.request.load_credentials", unexpected_credentials)
    with WitnessClient(
        config,
        openai_api_key="x" * 80 if mode == "api" else None,
        subscription="codex" if mode == "codex" else None,
        hotkey=key,
    ) as client:
        witness = client
        with pytest.raises(ValueError, match="session_id"):
            witness.create(
                stream=True, model="fixture", input="image", session_id="a\r\nX: injected"
            )


def test_sync_saved_recovery_bypasses_busy_capture_and_failure_releases_capacity(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = Event(), Event()
    key = Keypair.create_from_uri("//Alice")
    calls: dict[str, Snapshot] = {}
    opened: list[int] = []

    def start(invocation: RequestKey) -> Snapshot:
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, invocation.id))
        calls.setdefault(identifier, Snapshot(
            call=CallView(request=invocation, exchange_id=identifier, deadline=int(time.time()) + 60,
                          phase="issued" if invocation.request_id == 1 else "assigned"),
            hotkey=key.ss58_address, session={
                "id": identifier, "protocol": 2, "provider": "https://api.openai.com/v1/responses",
                "proof_key_sha256": "a" * 64, "miner": key.ss58_address, "round": "test-0",
            },
        ))
        return calls[identifier]

    @contextmanager
    def tunnel(identifier: str, *, deadline: float) -> Iterator[WebSocketTunnel]:
        yield WebSocketTunnel(MagicMock(), identifier)

    def exchange(tunnel: WebSocketTunnel, *args: object, **kwargs: object) -> bytes:
        number = calls[tunnel.identifier].call.request.request_id
        opened.append(number)
        if number == 0:
            entered.set()
            assert release.wait(3)
            raise RuntimeError("Fixture preparation failed")
        calls[tunnel.identifier].call.phase = "issued"
        return b""

    def recorded(*args: object, **kwargs: object) -> tuple[Session, int, bytes]:
        return cast(Session, {}), 200, event({"id": "resp_saved", "status": "completed", "output": []})

    with WitnessClient(config, hotkey=key, openai_api_key="x" * 80, parallel_calls=1, parallel_preparations=1) as client:
        for name, value in {"start": start, "snapshot": lambda identifier, **kwargs: calls[identifier],
                            "recorded": recorded, "tunnel": tunnel}.items():
            monkeypatch.setattr(client._gateway, name, value)
        monkeypatch.setattr("witness_client.client.exchange", exchange)
        with ThreadPoolExecutor(max_workers=3) as load:
            active = load.submit(client.create, model="fixture", input="image", request_id=0)
            try:
                assert entered.wait(2)
                queued = load.submit(client.create, model="fixture", input="image", request_id=2)
                saved = load.submit(client.create, model="fixture", input="image", request_id=1)
                assert saved.result(timeout=2).id == "resp_saved"
                assert opened == [0] and not queued.done()
            finally:
                release.set()
            with pytest.raises(openai.APIConnectionError):
                active.result(timeout=2)
            assert queued.result(timeout=2).id == "resp_saved" and opened == [0, 2]


@pytest.mark.parametrize("waiting_for", ["capture", "preparation"])
@pytest.mark.parametrize("unstarted_deadline", [None, 1060])
def test_queued_call_can_finish_after_waiting_longer_than_a_capture_window(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, waiting_for: str, unstarted_deadline: int | None,
) -> None:
    waiting = Event()
    clock = [1000.0]
    monkeypatch.setattr("witness_client.client.time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[0],
    ))

    class WaitingSemaphore(BoundedSemaphore):
        def __enter__(self, blocking: bool = True, timeout: float | None = None, /) -> bool:
            waiting.set()
            return super().__enter__(blocking, timeout)

    gate = WaitingSemaphore(1)
    gate.acquire()
    key = Keypair.create_from_uri("//Alice")
    identifier = str(uuid.uuid4())
    session: Session = {
        "id": identifier, "protocol": 2, "provider": "https://api.openai.com/v1/responses",
        "proof_key_sha256": "a" * 64, "miner": key.ss58_address, "round": "test-0",
    }
    current: Snapshot | None = None

    def start(invocation: RequestKey) -> Snapshot:
        nonlocal current
        current = Snapshot(
            call=CallView(request=invocation, exchange_id=identifier, deadline=unstarted_deadline, phase="assigned"),
            hotkey=key.ss58_address, session=dict(session),
        )
        return current

    @contextmanager
    def tunnel(identifier: str, *, deadline: float) -> Iterator[WebSocketTunnel]:
        yield WebSocketTunnel(MagicMock(), identifier)

    def exchange(*args: object, **kwargs: object) -> bytes:
        clock[0] += 18 + 45
        return b""

    def snapshot(identifier: str, **kwargs: object) -> Snapshot:
        assert current is not None
        current.call.phase = "issued"
        return current

    def recorded(*args: object, **kwargs: object) -> tuple[Session, int, bytes]:
        return session, 200, event({"id": "resp_queued", "status": "completed", "output": []})

    with WitnessClient(config, hotkey=key, openai_api_key="x" * 80) as client:
        monkeypatch.setattr(client, "_capture_slots" if waiting_for == "capture" else "_preparation", gate)
        for name, operation in {"start": start, "tunnel": tunnel, "snapshot": snapshot, "recorded": recorded}.items():
            monkeypatch.setattr(client._gateway, name, operation)
        monkeypatch.setattr("witness_client.client.exchange", exchange)
        with ThreadPoolExecutor(max_workers=1) as pool:
            queued = pool.submit(client.create, model="fixture", input="image")
            try:
                assert waiting.wait(2)
                clock[0] += 1801
            finally:
                gate.release()
            assert queued.result(timeout=3).id == "resp_queued"

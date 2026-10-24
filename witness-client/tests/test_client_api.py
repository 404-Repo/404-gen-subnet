"""Generation-bound calls respect preparation and capture concurrency."""

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

import pytest
from bittensor_wallet import Keypair

from witness_client import WitnessClient, WitnessConfig, WitnessError
from witness_client.calls import Generation, Invocation
from witness_client.pods import CallView, Snapshot
from witness_client.tunnel import WebSocketTunnel
from witness_client.types import (
    EarlyDisclosure,
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
        "round": "1",
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


@pytest.mark.parametrize("authorized, not_dispatched", [(False, False), (True, False), (True, True)])
def test_pod_failed_capture_without_non_dispatch_proof_remains_unresolved(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, authorized: bool, not_dispatched: bool,
) -> None:
    config = {**config, "transport": "pods-v1", "round": "test-0"}
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = str(uuid.uuid4())
    clock = [1000.0]
    snapshots: list[str] = []
    current: Snapshot | None = None

    def missing(_: Generation) -> None:
        raise WitnessError("No recording", status_code=404)

    def reserve(invocation: Invocation) -> Snapshot:
        nonlocal current
        current = Snapshot(
            call=CallView(
                request=invocation.request, exchange_id=identifier, owner_ordinal=0,
                deadline=1002, phase="failed", authorized=authorized, not_dispatched=not_dispatched,
            ),
            round="test-0", hotkey=key.ss58_address, session=None,
        )
        return current

    def snapshot(exchange_id: str) -> Snapshot:
        snapshots.append(exchange_id)
        assert current is not None
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
            "recording": missing, "reserve": reserve, "snapshot": snapshot,
            "retry": repeat, "ensure_pod": repeat, "dispatch": repeat, "tunnel": repeat,
        }.items():
            monkeypatch.setattr(witness._gateway, name, value)
        bound = witness.for_generation(b"PNG", 42)
        operations: tuple[Callable[[], object], ...] = (
            lambda: bound.create(stream=True, model="fixture", input="image"),
            lambda: witness.resume(identifier),
        )
        for operation in operations:
            with pytest.raises(WitnessError) as error:
                operation()
            assert error.value.code == "capture_in_progress"
            assert error.value.exchange_id == identifier and error.value.source == "gateway"
        assert len(snapshots) > 1 and set(snapshots) == {identifier}
        with pytest.raises(WitnessError) as unfinished:
            bound.finish(None)
        assert unfinished.value.code == "recording_unresolved"



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
    options: WitnessConfig = {**config, "transport": "pods-v1", "round": "test-0"}
    with WitnessClient(
        options,
        openai_api_key="x" * 80 if mode == "api" else None,
        subscription="codex" if mode == "codex" else None,
        hotkey=key,
    ) as client:
        witness = client.for_generation(b"PNG", 42)
        with pytest.raises(ValueError, match="session_id"):
            witness.create(
                stream=True, model="fixture", input="image", session_id="a\r\nX: injected"
            )

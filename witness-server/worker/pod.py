"""Persistent ordinary CPU witness Pod. All captures use the shared TLS verifier."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from channel import WorkerChannel
from storage import Store
from websockets.sync.client import connect
from websockets.sync.connection import Connection
from witness_client.calls import Invocation
from worker import PARAMETERS, PROVIDERS, relay, verify

from pod_gateway.budget import Budget
from pod_gateway.models import Scope, identifier, json_bytes, object_dict
from pod_gateway.records import Call, CallRecords
from pod_gateway.recovery import recover_issued
from pod_gateway.security import capture_token


class PodRuntime:
    def __init__(
        self,
        *,
        evidence: Store,
        records: CallRecords,
        budget: Budget,
        scope: Scope,
        start_id: str,
        pod_id: str,
        token: str,
        origin: str,
        proof_hash: str,
        round_end: int,
        spool: Path,
        max_captures: int = 2,
        idle_seconds: int = 1800,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        url = urlsplit(origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or url.username
        ):
            raise ValueError("Gateway must be an HTTPS origin")
        if idle_seconds < 1800 or not 1 <= max_captures <= 32:
            raise ValueError("Invalid Pod concurrency or idle lifetime")
        self.evidence, self.records, self.budget = evidence, records, budget
        self.scope, self.start_id, self.pod_id = scope, identifier(start_id), pod_id
        self.token, self.origin = token, f"wss://{url.netloc}"
        self.proof_hash, self.round_end = proof_hash, round_end
        self.spool, self.max_captures, self.idle_seconds = (
            spool,
            max_captures,
            idle_seconds,
        )
        self.clock = clock
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(
            max_workers=max_captures, thread_name_prefix="witness-capture"
        )
        self.active: dict[str, Future[None] | None] = {}
        self.accepting = self.draining = False
        self.last_used = self.clock()
        self.born = self.last_used
        self.spool.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.spool.stat().st_mode & 0o077:
            raise ValueError("Witness spool must be private")
        with self.lock:
            for directory in self.spool.iterdir():
                if not directory.is_dir() or directory.is_symlink():
                    continue
                try:
                    exchange_id = identifier(directory.name)
                except ValueError:
                    continue
                self.active[exchange_id] = None
                future = self.executor.submit(self._recover, exchange_id)
                self.active[exchange_id] = future
                future.add_done_callback(partial(self._done, exchange_id))

    def drain(self) -> None:
        with self.lock:
            self.accepting, self.draining = False, True

    def tick(self) -> bool:
        """True only after permanent local drain; pending uploads count as busy."""
        with self.lock:
            if time.time() >= self.round_end or (
                not self.active and self.clock() - self.last_used >= self.idle_seconds
            ):
                self.accepting, self.draining = False, True
            if self.draining and not self.active:
                return True
        return False

    def start_capture(self, invocation: Invocation, call: Call) -> bool:
        with self.lock:
            if call.exchange_id in self.active:
                return True
            if (
                not self.accepting
                or self.draining
                or len(self.active) >= self.max_captures
            ):
                return False
            if (
                call.pod_id != self.pod_id
                or call.start_id != self.start_id
                or call.capture_id is None
                or invocation.generation.round != self.scope.round
            ):
                return False
            self.active[call.exchange_id] = None
            self.last_used = self.clock()
            future = self.executor.submit(self._capture, invocation, call)
            self.active[call.exchange_id] = future
            future.add_done_callback(
                lambda result: self._done(call.exchange_id, result)
            )
            return True

    def _done(self, exchange_id: str, result: Future[None]) -> None:
        # Failure before a WS claim has no result to upload. After a claim,
        # _capture does not return until its outcome/partial evidence is durable.
        with self.lock:
            self.active.pop(exchange_id, None)
            self.last_used = self.clock()
        if result.exception() is not None:
            print(
                json.dumps(
                    {
                        "event": "capture_not_started",
                        "exchange_id": exchange_id,
                        "error_type": type(result.exception()).__name__,
                    }
                ),
                flush=True,
            )

    def _binding(self, invocation: Invocation, call: Call) -> dict[str, object]:
        return {
            "generation": invocation.generation.model_dump(),
            "generation_id": invocation.generation.id,
            "request_key": invocation.request.model_dump(),
            "call_id": invocation.request.id,
            "pod_id": self.pod_id,
            "pod_start_id": self.start_id,
            "proof_key_sha256": self.proof_hash,
            "pricing_sha256": self.budget.pricing_hash,
        }

    def _retry_upload(self, operation: Callable[[], None]) -> None:
        delay = 1
        while True:
            try:
                operation()
                return
            except Exception as error:  # noqa: BLE001 - retain pending evidence on all failures
                print(
                    json.dumps(
                        {
                            "event": "evidence_pending",
                            "error_type": type(error).__name__,
                        }
                    ),
                    flush=True,
                )
                time.sleep(delay)
                delay = min(30, delay * 2)

    def _recover(self, exchange_id: str) -> None:
        def recover() -> None:
            located = self.records.locate(self.scope, exchange_id)
            if located is None:
                raise RuntimeError("Spool has no inference record")
            invocation, call = located
            if call.pod_id != self.pod_id or call.start_id != self.start_id:
                raise RuntimeError("Spool belongs to a different capture allocation")
            call = recover_issued(
                self.records, self.budget, self.evidence.get, self.scope, invocation, call, self.proof_hash
            )
            if call.phase in ("issued", "failed"):
                self._cleanup(exchange_id)
                return
            if call.phase == "assigned":
                return
            if call.phase != "capturing":
                raise RuntimeError("Spool has no claimed capture")
            directory = self.spool / exchange_id
            metadata: dict[str, object] | None = None
            failure = "CaptureInterrupted"
            # Finishing retained TLS bytes cannot open a provider connection.
            if call.authorized:
                try:
                    metadata = verify("finish", directory, provider=invocation.request.provider)
                    metadata.update(
                        {
                            **self._binding(invocation, call),
                            "prefix": f"rounds/{self.scope.round}/{self.scope.hotkey}/{exchange_id}",
                            "round": self.scope.round,
                            "hotkey": self.scope.hotkey,
                            "exchange_id": exchange_id,
                            "verified_at": time.time(),
                            "client_bytes": (directory / "client.bin").stat().st_size,
                            "server_bytes": (directory / "server.bin").stat().st_size,
                        }
                    )
                except Exception as error:  # noqa: BLE001 - retain partial evidence on interrupted TLS
                    metadata, failure = None, type(error).__name__
            self._finalize(invocation, call, metadata, failure)

        self._retry_upload(recover)

    def _cleanup(self, exchange_id: str) -> None:
        def remove() -> None:
            with suppress(FileNotFoundError):
                shutil.rmtree(self.spool / identifier(exchange_id))

        self._retry_upload(remove)

    def _capture(self, invocation: Invocation, call: Call) -> None:
        assert call.capture_id is not None
        directory = self.spool / identifier(call.exchange_id)
        directory.mkdir(mode=0o700, exist_ok=True)
        prefix = f"rounds/{self.scope.round}/{self.scope.hotkey}/{call.exchange_id}"
        host, path = PROVIDERS[invocation.request.provider]
        session = {
            "id": call.exchange_id,
            "protocol": 2,
            "provider": f"https://{host}{path}",
            "proof_key_sha256": self.proof_hash,
            "round": self.scope.round,
            "miner": self.scope.hotkey,
        }
        token = capture_token(self.token, call.exchange_id, call.capture_id)
        # No automatic reconnect of a capture socket. Its durable claim is single-use.
        socket = connect(
            f"{self.origin}/worker/exchanges/{call.exchange_id}",
            additional_headers={
                "Authorization": "Bearer " + token,
                "x-witness-hotkey": self.scope.hotkey,
            },
            compression=None,
            proxy=None,
            max_size=2 * 1024 * 1024,
            max_queue=16,
            open_timeout=30,
            close_timeout=5,
        )
        with socket:
            channel = WorkerChannel(socket)
            metadata: dict[str, object] | None = None
            failure = "CaptureFailed"
            try:

                def ready() -> None:
                    self.evidence.put(prefix + "/session.json", json_bytes(session))
                    self.evidence.put(
                        prefix + "/status.json",
                        json_bytes({"status": "running", "authorized": False}),
                    )

                self._retry_upload(ready)
                socket.send(json_bytes({"type": "ready", "session": session}).decode())

                def authorize(body: bytes) -> None:
                    with self.lock:
                        if (
                            self.draining
                            or not self.accepting
                            or time.time() >= self.round_end
                        ):
                            raise RuntimeError("Pod is fenced before provider release")
                        selected = httpx.get(
                            self.origin.replace("wss://", "https://", 1)
                            + f"/worker/exchanges/{call.exchange_id}/selection",
                            headers={
                                "Authorization": "Bearer " + token,
                                "x-witness-hotkey": self.scope.hotkey,
                            },
                            timeout=10,
                            trust_env=False,
                        )
                        selected.raise_for_status()
                        if selected.json().get("selected") is not True:
                            raise RuntimeError(
                                "RunPod allocation is no longer selected"
                            )
                        self.budget.reserve(
                            self.scope, invocation, call.exchange_id, body
                        )
                        self.records.authorize(self.scope, invocation, self.pod_id)
                        if time.time() >= self.round_end:
                            raise RuntimeError("Round ended before provider release")

                metadata = relay(
                    self.evidence,
                    prefix,
                    time.monotonic() + call.deadline - time.time(),
                    directory,
                    partial(verify, provider=invocation.request.provider),
                    channel,
                    round_number=self.scope.round,
                    hotkey=self.scope.hotkey,
                    host=host,
                    before_release=authorize,
                    extra_metadata=self._binding(invocation, call),
                )
            except Exception as error:  # noqa: BLE001 - seal a captured outcome before cleanup
                failure = type(error).__name__
                # R2 can fail after the full provider response was captured. Finish
                # from that local spool, never by opening a new provider connection.
                if (directory / "authorization.json").is_file() and (
                    directory / "server.bin"
                ).is_file():
                    with suppress(Exception):
                        metadata = verify(
                            "finish", directory, provider=invocation.request.provider
                        )
                        metadata.update(
                            {
                                **self._binding(invocation, call),
                                "prefix": prefix,
                                "round": self.scope.round,
                                "hotkey": self.scope.hotkey,
                                "exchange_id": call.exchange_id,
                                "verified_at": time.time(),
                                "client_bytes": (directory / "client.bin")
                                .stat()
                                .st_size,
                                "server_bytes": (directory / "server.bin")
                                .stat()
                                .st_size,
                            }
                        )
            self._finalize(invocation, call, metadata, failure)

    def _finalize(self, invocation: Invocation, call: Call, metadata: dict[str, object] | None, failure: str) -> None:
        directory = self.spool / identifier(call.exchange_id)
        prefix = f"rounds/{self.scope.round}/{self.scope.hotkey}/{call.exchange_id}"
        if metadata is not None:
            final = metadata

            def issued() -> None:
                located = self.records.locate(self.scope, call.exchange_id)
                if located is None:
                    raise RuntimeError("Capture record is missing")
                current = located[1]
                if current.phase == "issued":
                    return
                for name in (
                    "client.bin",
                    "server.bin",
                    "authorization-server.bin",
                    "claim.json",
                    "authorization.json",
                    "handshake.json",
                    "request.json",
                    "response.sse",
                ):
                    file = directory / name
                    if file.is_file():
                        self.evidence.put(prefix + "/" + name, file.read_bytes())
                recovered = recover_issued(
                    self.records, self.budget, self.evidence.get, self.scope, invocation, current, self.proof_hash
                )
                if recovered.phase == "issued":
                    return
                manifest = json_bytes(final)
                self.evidence.put(prefix + "/manifest.json", manifest)
                self.evidence.put(
                    prefix + "/status.json",
                    json_bytes(
                        {
                            "status": "issued",
                            "authorized": True,
                            "provider_status": final["provider_status"],
                            "usage": final.get("usage"),
                            "evidence_expires": int(
                                (time.time() + 7 * 86400) * 1000
                            ),
                        }
                    ),
                )
                self.budget.settle(self.scope, invocation, final.get("usage"))
                self.records.issued(self.scope, invocation, self.pod_id, manifest)

            self._retry_upload(issued)
        else:

            def failed() -> None:
                located = self.records.locate(self.scope, call.exchange_id)
                if located is None:
                    raise RuntimeError("Capture record is missing")
                recovered = recover_issued(
                    self.records, self.budget, self.evidence.get, self.scope, invocation, located[1], self.proof_hash
                )
                if recovered.phase == "issued":
                    return
                for file in directory.iterdir():
                    if file.name in {
                        "client.bin",
                        "server.bin",
                        "authorization-server.bin",
                        "claim.json",
                        "authorization.json",
                        "handshake.json",
                        "request.json",
                    }:
                        self.evidence.put(
                            prefix + "/" + file.name, file.read_bytes()
                        )
                self.budget.settle(self.scope, invocation, None)
                self.records.failed(self.scope, invocation, self.pod_id, failure)
                self.evidence.put(
                    prefix + "/status.json",
                    json_bytes({"status": "failed", "error_type": failure}),
                )

            self._retry_upload(failed)
        self._cleanup(call.exchange_id)

    def _send(self, socket: Connection, data: dict[str, object]) -> None:
        socket.send(json_bytes(data).decode())

    def run(self) -> None:
        delay = 1
        try:
            while True:
                try:
                    if self.tick():
                        return
                    with connect(
                        f"{self.origin}/control/{self.scope.hotkey}/{self.start_id}",
                        additional_headers={
                            "Authorization": "Bearer " + self.token,
                            "x-witness-pod": self.pod_id,
                        },
                        compression=None,
                        proxy=None,
                        max_size=16384,
                        max_queue=16,
                        open_timeout=30,
                        close_timeout=5,
                        ping_interval=15,
                        ping_timeout=20,
                    ) as socket:
                        delay = 1
                        while True:
                            try:
                                value = object_dict(json.loads(socket.recv(timeout=5)))
                                kind = value.get("type")
                                if kind == "activate":
                                    with self.lock:
                                        self.accepting = not self.draining
                                elif kind == "drain":
                                    self.drain()
                                    self._send(
                                        socket,
                                        {
                                            "type": "reply",
                                            "id": value["id"],
                                            "drained": False,
                                        },
                                    )
                                elif kind == "capture":
                                    accepted = self.start_capture(
                                        Invocation.model_validate(value["invocation"]),
                                        Call.model_validate(value["call"]),
                                    )
                                    self._send(
                                        socket,
                                        {
                                            "type": "reply",
                                            "id": value["id"],
                                            "accepted": accepted,
                                        },
                                    )
                                else:
                                    raise ValueError("Unknown control command")
                            except TimeoutError:
                                pass
                            if self.tick():
                                self._send(socket, {"type": "drained"})
                                return
                            with self.lock:
                                self._send(
                                    socket,
                                    {
                                        "type": "heartbeat",
                                        "ready": self.accepting and not self.draining,
                                        "active": len(self.active),
                                        "proof_key_sha256": self.proof_hash,
                                        "pricing_sha256": self.budget.pricing_hash,
                                    },
                                )
                except Exception as error:  # noqa: BLE001 - reconnect without replaying capture
                    with self.lock:
                        self.accepting = False
                    print(
                        json.dumps(
                            {
                                "event": "control_reconnect",
                                "error_type": type(error).__name__,
                            }
                        ),
                        flush=True,
                    )
                    time.sleep(delay)
                    delay = min(30, delay * 2)
        finally:
            self.drain()
            self.executor.shutdown(wait=True)


def main() -> None:
    from storage import R2Store

    from pod_gateway.state import R2StateStore

    state = R2StateStore.connect(
        endpoint=os.environ["R2_ENDPOINT"],
        access_key=os.environ["R2_ACCESS_KEY_ID"],
        secret_key=os.environ["R2_SECRET_ACCESS_KEY"],
        bucket=os.environ["R2_BUCKET"],
    )
    pricing = Path(os.environ["WITNESS_PRICING_FILE"]).read_bytes()
    if hashlib.sha256(pricing).hexdigest() != os.environ["WITNESS_PRICING_SHA256"]:
        raise ValueError("Witness image has different frozen pricing bytes")
    runtime = PodRuntime(
        evidence=R2Store(),
        records=CallRecords(state),
        budget=Budget(state, pricing),
        scope=Scope(
            os.environ["WITNESS_DEPLOYMENT"],
            os.environ["WITNESS_ROUND"],
            os.environ["WITNESS_HOTKEY"],
        ),
        start_id=os.environ["WITNESS_START_ID"],
        pod_id=os.environ["RUNPOD_POD_ID"],
        token=os.environ["WITNESS_POD_CAPABILITY"],
        origin=os.environ["WITNESS_GATEWAY_URL"],
        proof_hash=hashlib.sha256(
            (PARAMETERS / "verifying.bin").read_bytes()
        ).hexdigest(),
        round_end=int(os.environ["WITNESS_ROUND_END"]),
        spool=Path(os.environ.get("WITNESS_SPOOL", "/var/lib/witness/spool")),
        max_captures=int(os.environ.get("WITNESS_MAX_CAPTURES", "2")),
        idle_seconds=int(os.environ.get("WITNESS_IDLE_SECONDS", "1800")),
    )
    signal.signal(signal.SIGTERM, lambda *_: runtime.drain())
    signal.signal(signal.SIGINT, lambda *_: runtime.drain())
    runtime.run()


if __name__ == "__main__":
    main()

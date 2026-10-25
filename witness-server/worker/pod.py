"""Persistent ordinary CPU witness Pod. All captures use the shared TLS verifier."""

from __future__ import annotations

import asyncio
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

from channel import Channel
from pod_gateway.budget import Budget, BudgetNotReserved
from pod_gateway.models import Scope, identifier, json_bytes, object_dict
from pod_gateway.records import Call, CallRecords
from pod_gateway.recovery import recover_issued
from pod_gateway.renderer import render_service
from pod_gateway.selection import Selection
from pod_gateway.state import WriteUncertain
from storage import Store
from timing import identify_exchange, measure, timed
from websockets.sync.client import connect
from websockets.sync.connection import Connection
from witness_client.calls import Invocation
from witness_client.gateway import _GatewayConnection
from witness_client.state import save
from worker import PARAMETERS, PROVIDERS, relay, verify


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
        selection: Selection | None = None,
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
        self.selection = selection
        self.certificate: str | None = None
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(
            max_workers=max_captures, thread_name_prefix="witness-capture"
        )
        self.active: dict[str, Future[None] | None] = {}
        self.rendering = 0
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
            if not self.active and not self.rendering and self.clock() - self.last_used >= self.idle_seconds:
                self.accepting, self.draining = False, True
            if self.draining and not self.active and not self.rendering:
                return True
        return False

    def selected(self) -> bool:
        with self.lock:
            return (self.accepting and not self.draining and (self.selection is None or
                    self.selection.selected(self.scope, self.start_id, self.pod_id)))

    def start_capture(self, invocation: Invocation, call: Call, channel: Channel) -> bool:
        with self.lock:
            if call.exchange_id in self.active:
                return False
            if (
                not self.selected()
                or self.draining
                or len(self.active) >= self.max_captures
                or time.time() >= self.round_end
            ):
                return False
            if (
                call.pod_id != self.pod_id
                or call.start_id != self.start_id
                or call.capture_id is None
                or invocation.generation.round != self.scope.round
            ):
                return False
            directory = self.spool / identifier(call.exchange_id)
            directory.mkdir(mode=0o700, exist_ok=True)
            save(directory / "pending-claim", call.capture_id.encode())
            recover = False
            try:
                if not self.records.claim(self.scope, invocation, self.pod_id, call.capture_id):
                    self._cleanup(call.exchange_id)
                    return False
            except WriteUncertain:
                recover = True
            self.active[call.exchange_id] = None
            self.last_used = self.clock()
            future = (self.executor.submit(self._recover, call.exchange_id) if recover
                      else self.executor.submit(self._capture, invocation, call, channel))
            self.active[call.exchange_id] = future
            future.add_done_callback(
                lambda result: self._done(call.exchange_id, result)
            )
            return not recover

    @timed("pod_capture_done")
    def _done(self, exchange_id: str, result: Future[None]) -> None:
        identify_exchange(exchange_id)
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
                with measure("evidence_retry_wait"):
                    time.sleep(delay)
                delay = min(30, delay * 2)

    @timed("pod_recovery")
    def _recover(self, exchange_id: str) -> None:
        identify_exchange(exchange_id)
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
            directory = self.spool / exchange_id
            pending = directory / "pending-claim"
            not_dispatched = pending.is_file() and pending.read_text() == call.capture_id
            if not_dispatched and call.phase == "assigned":
                assert call.capture_id is not None
                if not self.records.claim(self.scope, invocation, self.pod_id, call.capture_id):
                    raise RuntimeError("Capture claim changed during recovery")
                call = call.model_copy(update={"phase": "capturing"})
            if call.phase == "assigned":
                return
            if call.phase != "capturing":
                raise RuntimeError("Spool has no claimed capture")
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
                if metadata is None:
                    # A dead process cannot establish how much of a paid response
                    # was lost. Keep this attempt pending so it cannot be retried.
                    self.evidence.put(
                        f"rounds/{self.scope.round}/{self.scope.hotkey}/{exchange_id}/status.json",
                        json_bytes({
                            "status": "running", "authorized": True,
                            "usage_state": "unknown", "error_type": failure,
                        }),
                    )
                    return
            self._finalize(invocation, call, metadata, failure, not_dispatched=not_dispatched)

        self._retry_upload(recover)

    def _cleanup(self, exchange_id: str) -> None:
        def remove() -> None:
            with suppress(FileNotFoundError):
                shutil.rmtree(self.spool / identifier(exchange_id))

        self._retry_upload(remove)

    PUBLIC_EVIDENCE = frozenset({
        "client.bin", "server.bin", "authorization-server.bin", "claim.json", "authorization.json",
        "handshake.json", "request.json", "response.sse", "session.json",
    })

    def artifact_key(self, exchange_id: str, name: str, digest: str) -> str:
        if (name not in self.PUBLIC_EVIDENCE | {"manifest.json"} or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("Invalid public evidence object")
        return (f"evidence/v1/{self.scope.deployment}/{self.scope.round}/{self.scope.hotkey}/"
                f"{identifier(exchange_id)}/{digest}/{name}")

    def publish(self, exchange_id: str, metadata: dict[str, object]) -> dict[str, object]:
        prefix = f"rounds/{self.scope.round}/{self.scope.hotkey}/{identifier(exchange_id)}/"
        objects = {}
        for name in sorted(self.PUBLIC_EVIDENCE):
            raw = self.evidence.get(prefix + name)
            if raw is None:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            key = self.artifact_key(exchange_id, name, digest)
            self.evidence.put(key, raw)
            objects[name] = {"key": key, "sha256": digest, "size": len(raw)}
        metadata["artifacts"] = objects
        return metadata

    @timed("pod_capture")
    def _capture(self, invocation: Invocation, call: Call, channel: Channel) -> None:
        identify_exchange(call.exchange_id)
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
        metadata: dict[str, object] | None = None
        failure = "CaptureFailed"
        not_dispatched = True
        finalized = False
        try:
            # No provider connection may open while a pending claim can be recovered as unstarted.
            (directory / "pending-claim").unlink(missing_ok=True)

            def ready() -> None:
                self.evidence.put(prefix + "/session.json", json_bytes(session))
                self.evidence.put(
                    prefix + "/status.json",
                    json_bytes({"status": "running", "authorized": False}),
                )

            self._retry_upload(ready)

            def authorize(body: bytes) -> None:
                nonlocal not_dispatched
                with self.lock:
                    if (
                        self.draining
                        or not self.accepting
                        or time.time() >= self.round_end
                    ):
                        raise RuntimeError("Pod is fenced before provider release")
                    if not self.selected():
                        raise RuntimeError("RunPod allocation is no longer selected")
                    # An uncertain reservation write can still become durable.
                    not_dispatched = False
                    try:
                        self.budget.reserve(
                            self.scope, invocation, call.exchange_id, body
                        )
                    except BudgetNotReserved:
                        not_dispatched = True
                        raise
                    self.records.authorize(self.scope, invocation, self.pod_id, exchange_id=call.exchange_id)
                    if time.time() >= self.round_end or not self.selected():
                        raise RuntimeError("Round ended before provider release")

            def complete(final: dict[str, object]) -> None:
                nonlocal finalized
                self._finalize(invocation, call, final, failure)
                finalized = True

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
                seal=partial(self.publish, call.exchange_id),
                before_delivery=complete,
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
        if not finalized:
            self._finalize(invocation, call, metadata, failure, not_dispatched=not_dispatched)
        with suppress(OSError):
            channel.finish()

    def _finalize(
        self, invocation: Invocation, call: Call, metadata: dict[str, object] | None, failure: str, *,
        not_dispatched: bool = False,
    ) -> None:
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
                self.publish(call.exchange_id, final)
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
                self.budget.settle(self.scope, invocation, final.get("usage"), exchange_id=call.exchange_id)
                self.records.issued(self.scope, invocation, self.pod_id, manifest, exchange_id=call.exchange_id)

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
                self.budget.settle(self.scope, invocation, None, exchange_id=call.exchange_id)
                self.records.failed(
                    self.scope, invocation, self.pod_id, failure, exchange_id=call.exchange_id,
                    not_dispatched=not_dispatched,
                )
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
                        create_connection=_GatewayConnection,
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
                                        "certificate": self.certificate,
                                        "active": len(self.active) + self.rendering,
                                        "proof_key_sha256": self.proof_hash,
                                        "pricing_sha256": self.budget.pricing_hash,
                                    },
                                )
                except Exception as error:  # noqa: BLE001 - reconnect without replaying capture
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
    from aiohttp import web
    from pod_gateway.certs import pod_context
    from pod_gateway.direct import Witness
    from pod_gateway.state import R2StateStore
    from storage import R2Store

    state = R2StateStore.connect(
        endpoint=os.environ["R2_ENDPOINT"],
        access_key=os.environ["R2_ACCESS_KEY_ID"],
        secret_key=os.environ["R2_SECRET_ACCESS_KEY"],
        bucket=os.environ["R2_BUCKET"],
    )
    pricing = Path(os.environ["WITNESS_PRICING_FILE"]).read_bytes()
    if hashlib.sha256(pricing).hexdigest() != os.environ["WITNESS_PRICING_SHA256"]:
        raise ValueError("Witness image has different frozen pricing bytes")
    evidence = R2Store()
    runtime = PodRuntime(
        evidence=evidence,
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
        selection=Selection(state),
    )
    signal.signal(signal.SIGTERM, lambda *_: runtime.drain())
    signal.signal(signal.SIGINT, lambda *_: runtime.drain())
    tls, runtime.certificate = pod_context(runtime.spool.parent / "tls")

    async def serve() -> None:
        service = Witness(runtime, frozenset(json.loads(os.environ["WITNESS_PROMPT_HASHES"])), evidence.url)
        runner = web.AppRunner(service.application(), handler_cancellation=False)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("WITNESS_PORT", "8000")), ssl_context=tls)
        await site.start()
        try:
            await asyncio.to_thread(runtime.run)
        finally:
            await runner.cleanup()

    with render_service(runtime.drain):
        asyncio.run(serve())


if __name__ == "__main__":
    main()

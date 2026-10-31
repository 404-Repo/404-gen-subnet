"""Authorize, capture and record OpenAI exchanges for the Pod worker."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import select
import socket
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from time import time_ns
from typing import cast

from channel import Channel
from identity import Provider, miner_prefix
from storage import MAX_BYTES, Store, json_bytes
from timing import emit, identify_exchange, measure, progress, timed, transport
from timing import exchange_id as _exchange_id
from timing import forward as _verifier_timings
from timing import started as timing_started
from websockets.exceptions import WebSocketException
from witness_client.state import save

HERE = Path(__file__).resolve().parent
WITNESS = HERE.parent / "verifier"
PARAMETERS = Path(os.environ.get("WITNESS_PARAMETERS", str(WITNESS / "params")))
CLIENT_HELLO_TIMEOUT = 60
AUTHORIZATION_TIMEOUT = 300
PROVIDERS: dict[Provider, tuple[str, str]] = {
    "codex": ("chatgpt.com", "/backend-api/codex/responses"),
    "api": ("api.openai.com", "/v1/responses"),
}
Verifier = Callable[[str, Path], dict[str, object]]


def record_failure(
    directory: Path,
    error: Exception | None,
    stage: str,
    *,
    reason: str | None = None,
    context: dict[str, object] | None = None,
    verifier: dict[str, object] | None = None,
) -> None:
    """Spool the first failure without exception messages, locals or source lines."""
    path = directory / "failure.json"
    data: dict[str, object] = json.loads(path.read_bytes()) if path.is_file() else {}
    if "diagnostic" not in data:
        data["schema"] = 1
        frames: list[dict[str, object]] = []
        trace = None if error is None else error.__traceback__
        while trace is not None:
            frames.append({
                "file": "/".join(trace.tb_frame.f_code.co_filename.replace("\\", "/").split("/")[-2:]),
                "function": trace.tb_frame.f_code.co_name,
                "line": trace.tb_lineno,
            })
            trace = trace.tb_next
        error_reason = getattr(error, "witness_reason", None)
        data["diagnostic"] = {
            "timestamp_ns": time_ns(),
            "stage": stage,
            "reason": reason or (error_reason if isinstance(error_reason, str) else f"{stage}_failed"),
            "error_type": None if error is None else type(error).__name__,
            "cause_type": type(error.__cause__).__name__ if error is not None and error.__cause__ is not None else None,
            "stack": frames[-32:],
            "verifier": verifier,
        }
        sources = {
            "pod.py": HERE / "pod.py", "worker.py": HERE / "worker.py", "verify.mjs": HERE / "verify.mjs",
            "verifier/verify.mjs": WITNESS / "verify.mjs",
            "pod_gateway/budget.py": HERE.parent.parent / "witness-gateway" / "pod_gateway" / "budget.py",
        }
        data["runtime"] = {
            "python_version": platform.python_version(),
            "pod_spec_sha256": os.environ.get("WITNESS_POD_SPEC"),
            "source_sha256": {name: hashlib.sha256(source.read_bytes()).hexdigest() for name, source in sources.items()},
        }
    if context is not None:
        data.update(context)
    save(path, json_bytes(data))


def complete_records(data: bytes | bytearray) -> bool:
    offset = 0
    while offset < len(data):
        if len(data) - offset < 5:
            return False
        offset += 5 + int.from_bytes(data[offset + 3 : offset + 5], "big")
    return offset == len(data)


def verifier_failure(stderr: bytes, mode: str, returncode: int | None) -> dict[str, object]:
    """Read the verifier's content-free failure event, ignoring other stderr."""
    details: dict[str, object] = {"mode": mode, "returncode": returncode}
    for line in stderr.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict) or value.get("event") != "witness_verifier_failure" or value.get("mode") != mode:
            continue
        error_type, frames = value.get("error_type"), value.get("stack")
        if not isinstance(error_type, str) or not error_type.isidentifier() or not isinstance(frames, list):
            continue
        stack = []
        for frame in frames[:32]:
            if not isinstance(frame, dict):
                continue
            file, function, number, column = (frame.get(key) for key in ("file", "function", "line", "column"))
            if (not isinstance(file, str) or not re.fullmatch(r"(?:worker|verifier)/[A-Za-z0-9_.-]+\.mjs", file)
                    or (function is not None and (not isinstance(function, str)
                        or not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_.$]*", function)))
                    or type(number) is not int or number <= 0 or type(column) is not int or column <= 0):
                continue
            stack.append({"file": file, "function": function, "line": number, "column": column})
        details.update(error_type=error_type, stack=stack)
    return details


def verify(
    mode: str, directory: Path, *, provider: Provider | None = None
) -> dict[str, object]:
    returncode = None
    stderr = b""
    reason = "verifier_process_failed"
    try:
        with measure("verifier_subprocess"):
            result = subprocess.run(
                [
                    "node",
                    str(HERE / "verify.mjs"),
                    mode,
                    str(directory),
                    str(PARAMETERS),
                    *([provider] if provider is not None else []),
                ],
                capture_output=True,
                timeout=180,
                check=False,
                env={
                    **os.environ,
                    "NODE_OPTIONS": "--max-old-space-size=384",
                    "WITNESS_EXCHANGE_ID": _exchange_id(),
                    "WITNESS_VERIFIER_MODE": mode,
                },
            )
        returncode = result.returncode
        stderr = result.stderr
        _verifier_timings(result.stderr)
        if result.returncode:
            if mode == "finish" and result.returncode != 2:
                raise RuntimeError("Witness verifier did not complete")
            reason = "verification_rejected" if mode == "finish" else "verifier_process_failed"
            raise ValueError("Witness verification rejected the exchange")
        try:
            return cast(dict[str, object], json.loads(result.stdout))
        except ValueError as error:
            reason = "verifier_invalid_result"
            raise RuntimeError("Witness verifier returned an invalid result") from error
    except Exception as error:
        if isinstance(error, subprocess.TimeoutExpired):
            reason = "verifier_timeout"
        elif isinstance(error, OSError):
            reason = "verifier_unavailable"
        record_failure(directory, error, "verifier_" + mode, reason=reason,
                       verifier=verifier_failure(stderr, mode, returncode))
        raise


@timed("relay")
def relay(
    store: Store,
    prefix: str,
    deadline: float | None,
    directory: Path,
    verifier: Verifier,
    channel: Channel,
    *,
    round_number: str,
    hotkey: str,
    host: str,
    storage_prefix: str = "",
    port: int = 443,
    before_release: Callable[[bytes], float | None] | None = None,
    extra_metadata: dict[str, object] | None = None,
    seal: Callable[[dict[str, object]], object] | None = None,
    before_delivery: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Finish TLS promptly, then hold HTTP until a durable authorization exists.

    After release, provider capture is independent of the miner. The complete
    response and its usage become durable before its ciphertext is delivered.
    """
    if prefix.rsplit("/", 1)[0] != storage_prefix + miner_prefix(round_number, hotkey):
        raise ValueError("Transcript path differs from authenticated identity")
    identify_exchange(prefix.rsplit("/", 1)[1])
    started = time.monotonic()
    hello_deadline = started + CLIENT_HELLO_TIMEOUT
    authorization_deadline = started + AUTHORIZATION_TIMEOUT
    if deadline is not None:
        hello_deadline = min(deadline, hello_deadline)
        authorization_deadline = min(deadline, authorization_deadline)
    client = bytearray()
    server = bytearray()
    issued = False
    handshake = False
    handshake_bytes = 0
    stage = "provider_connect"
    reason: str | None = None
    try:
        with measure("provider_tcp_connect"):
            connection = socket.create_connection((host, port), timeout=10)
        with connection as provider:
            # Read one whole TLS record, then let the shared gate validate it.
            with measure("client_hello_wait"):
                stage = "client_hello_wait"
                while len(client) < 5 or len(client) < 5 + int.from_bytes(
                    client[3:5], "big"
                ):
                    if time.monotonic() >= hello_deadline:
                        reason = "client_hello_timeout"
                        raise TimeoutError("ClientHello deadline reached")
                    chunk = channel.recv(hello_deadline)
                    if not chunk:
                        raise ValueError("Missing ClientHello")
                    client.extend(chunk)
                    if (
                        len(client) >= 5
                        and 5 + int.from_bytes(client[3:5], "big") > 16645
                    ):
                        raise ValueError("Oversized ClientHello")
            stage = "client_hello_persist"
            (directory / "client.bin").write_bytes(client)
            with measure("verify_hello"):
                stage = "hello_verify"
                gate = verifier("hello", directory)
            released = int(str(gate["released"]))
            stage = "provider_hello_release"
            provider.sendall(client[:released])

            handshake_started = timing_started()
            authorization_started = handshake_started
            # The server's first TLS flight is safe to deliver. Verify and
            # release Finished first, then verify and release the HTTP request.
            while time.monotonic() < authorization_deadline:
                if select.select([provider], [], [], 0)[0]:
                    stage = "provider_handshake_read"
                    chunk = provider.recv(65536)
                    if not chunk:
                        transport("capture_failed", _exchange_id(), reason="provider_closed_before_authorization")
                        reason = "provider_closed_before_authorization"
                        raise ValueError("Provider closed before authorization")
                    server.extend(chunk)
                    if len(server) > 128 * 1024:
                        raise ValueError("Server handshake exceeds limit")
                    # Before request authorization the shared verifier permits
                    # only TLS setup and its fixed, unauthenticated HEAD probe.
                    stage = "miner_handshake_delivery"
                    channel.send(chunk)
                try:
                    stage = "miner_message_wait"
                    chunk = channel.recv(
                        min(authorization_deadline, time.monotonic() + 0.05)
                    )
                    if not chunk:
                        reason = "miner_left_before_authorization"
                        raise ValueError("Miner left before authorization")
                    client.extend(chunk)
                    if len(client) > MAX_BYTES:
                        raise ValueError("Client traffic exceeds limit")
                except TimeoutError:
                    pass
                claim_name = "claim.json" if handshake else "handshake-claim.json"
                raw_claim = channel.claim("authorize" if handshake else "handshake")
                if raw_claim is None:
                    continue
                stage = "handshake_claim" if not handshake else "authorization_claim"
                claim = json.loads(raw_claim)
                size = claim.get("client_bytes")
                if (
                    not isinstance(size, int)
                    or not 0 < size <= MAX_BYTES
                    or len(client) > size
                ):
                    raise ValueError("Invalid authorized request size")
                if len(client) < size:
                    transport("claim_wait", _exchange_id(), kind="client", size=len(client), expected_bytes=size)
                    continue
                if not complete_records(server):
                    transport("claim_wait", _exchange_id(), kind="server", size=len(server), reason="partial_tls_record")
                    continue
                if handshake and len(server) == handshake_bytes:
                    transport("claim_wait", _exchange_id(), kind="server", reason="session_ticket")
                    continue  # Wait for a ticket to confirm the committed key.
                transport("claim_ready", _exchange_id(), kind=claim_name, size=len(client), expected_bytes=size)
                with measure("claim_spool_persistence"):
                    stage = "claim_persist"
                    (directory / "client.bin").write_bytes(client)
                    (directory / "server.bin").write_bytes(server)
                    (directory / claim_name).write_bytes(raw_claim)
                if not handshake:
                    emit("handshake_capture_wait", handshake_started)
                    with measure("verify_handshake"):
                        stage = "handshake_verify"
                        verifier("handshake", directory)
                    with measure("handshake_persistence"):
                        stage = "handshake_persist"
                        store.put(
                            prefix + "/handshake.json",
                            (directory / "handshake.json").read_bytes(),
                        )
                    handshake_bytes = len(server)
                    handshake = True
                    stage = "handshake_notification"
                    channel.notify(
                        {"status": "running", "handshook": True, "authorized": False}
                    )
                    stage = "provider_probe_release"
                    provider.sendall(client[released:])
                    released = len(client)
                    authorization_started = timing_started()
                    continue
                emit("authorization_claim_wait", authorization_started)
                with measure("verify_authorize"):
                    stage = "authorization_verify"
                    verifier("authorize", directory)
                # These writes complete before any billable HTTP bytes leave.
                with measure("authorization_persistence"):
                    stage = "authorization_persist"
                    for name in (
                        "client.bin",
                        "server.bin",
                        "claim.json",
                        "authorization.json",
                        "request.json",
                    ):
                        store.put(prefix + "/" + name, (directory / name).read_bytes())
                    store.put(
                        prefix + "/status.json",
                        json_bytes(
                            {"status": "running", "handshook": True, "authorized": True}
                        ),
                    )
                if time.monotonic() >= authorization_deadline:
                    reason = "authorization_timeout"
                    raise TimeoutError("Request authorization deadline reached")
                if before_release is not None:
                    stage = "dispatch_reservation"
                    (directory / "authorization-server.bin").write_bytes(server)
                    store.put(prefix + "/authorization-server.bin", bytes(server))
                    capture_deadline = before_release((directory / "request.json").read_bytes())
                    if capture_deadline is not None:
                        deadline = capture_deadline
                    if time.monotonic() >= authorization_deadline:
                        reason = "authorization_timeout"
                        raise TimeoutError("Request authorization deadline reached")
                if deadline is None:
                    raise ValueError("Capture deadline is missing")
                # Durable authorization survives loss of its live acknowledgement.
                with suppress(OSError, WebSocketException):
                    channel.notify(
                        {"status": "running", "handshook": True, "authorized": True}
                    )
                handshake_bytes = len(server)
                with measure("provider_request_release"):
                    stage = "provider_request_release"
                    provider.sendall(client[released:])
                    break
            else:
                reason = "authorization_timeout"
                raise TimeoutError("Request authorization deadline reached")

            response_started = timing_started()
            first_response = True
            # Do not consume client messages or let its disconnect cancel GPT.
            with measure("provider_response_capture"):
                stage = "provider_response_capture"
                while time.monotonic() < deadline:
                    progress()
                    if not select.select([provider], [], [], 0.2)[0]:
                        continue
                    chunk = provider.recv(65536)
                    if not chunk:
                        break
                    if first_response:
                        emit("provider_first_ciphertext_wait", response_started)
                        first_response = False
                    server.extend(chunk)
                    if len(server) > MAX_BYTES:
                        raise ValueError("Provider traffic exceeds 16 MiB")
                else:
                    reason = "provider_exchange_timeout"
                    raise TimeoutError("Provider exchange deadline reached")
        stage = "final_transcript_persist"
        (directory / "server.bin").write_bytes(server)
        with measure("final_transcript_persistence"):
            store.put(prefix + "/server.bin", bytes(server))
        with measure("verify_finish"):
            stage = "response_verify"
            metadata = verifier("finish", directory)
        metadata.update(
            {
                "round": round_number,
                "hotkey": hotkey,
                "exchange_id": prefix.rsplit("/", 1)[1],
            }
        )
        if extra_metadata is not None:
            metadata.update(extra_metadata)
        with measure("evidence_persistence"):
            stage = "evidence_persist"
            for name in ("request.json", "response.sse"):
                store.put(prefix + "/" + name, (directory / name).read_bytes())
            if seal is not None:
                seal(metadata)
            with measure("manifest_persistence"):
                stage = "manifest_persist"
                store.put(prefix + "/manifest.json", json_bytes(metadata))
            with measure("final_status_persistence"):
                stage = "final_status_persist"
                store.put(
                    prefix + "/status.json",
                    json_bytes(
                        {
                            "status": "issued",
                            "authorized": True,
                            "usage": metadata.get("usage"),
                            "provider_status": metadata["provider_status"],
                            "usage_state": "recorded" if metadata.get("usage") is not None else "unknown",
                            "evidence_expires": int((time.time() + 7 * 86400) * 1000),
                        }
                    ),
                )
        issued = True
        if before_delivery is not None:
            stage = "call_finalize"
            before_delivery(metadata)
        # Capture is durable before delivery. A disconnected miner can recover it.
        with measure("delivery"), suppress(OSError, WebSocketException):
            channel.send(bytes(server[handshake_bytes:]))
            channel.finish()
        return metadata
    except Exception as error:
        transport("capture_failed", _exchange_id(), reason=stage, error_type=type(error).__name__)
        if stage == "provider_connect":
            reason = "provider_connection_failed"
        record_failure(directory, error, stage, reason=reason, context={
            **(extra_metadata or {}), "round": round_number, "hotkey": hotkey,
            "exchange_id": prefix.rsplit("/", 1)[1],
        })
        raise
    finally:
        # Ordinary Pods retain the spool for upload/finalization recovery. This
        # never opens another provider connection or repeats a paid request.
        with suppress(OSError):
            (directory / "client.bin").write_bytes(client)
            (directory / "server.bin").write_bytes(server)
        if not issued:
            with suppress(OSError, WebSocketException):
                channel.finish()

"""Manually replay a round/hotkey from Git and saved R2 model/render outcomes."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import signal
import subprocess  # nosec B404
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict
from pathlib import Path
from typing import Any

from renderer_client import Renderer, RenderOptions, RenderRejected, RenderUnavailable
from renderer_client.client import MAX_RESPONSE_BYTES
from witness_client.calls import RequestKey

from .render_recovery import (
    MAX_MISSING_RENDER_CALLS,
    MAX_MISSING_RENDER_RETRIES,
    MAX_RECOVERY_ATTEMPTS,
    MissingRenderAttempt,
    MissingRenderOutcome,
    RenderFailure,
    RenderRecoverySearch,
)
from .request_replay import ReplaySource


INDEX_CACHE = Path(__file__).resolve().parents[1] / ".reports/replay-indexes"


def index_cache_directory(endpoint: str, bucket: str) -> Path:
    identity = hashlib.sha256(json.dumps([endpoint.rstrip("/"), bucket]).encode()).hexdigest()
    return INDEX_CACHE / identity


class ReplayProcesses:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active: set[subprocess.Popen[str]] = set()
        self.stopped = False

    @staticmethod
    def kill(process: subprocess.Popen[str]) -> None:
        # Reap an exited leader before signalling any remaining group members.
        process.poll()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def add(self, process: subprocess.Popen[str]) -> None:
        with self.lock:
            self.active.add(process)
            if self.stopped:
                self.kill(process)

    def discard(self, process: subprocess.Popen[str]) -> None:
        with self.lock:
            self.active.discard(process)

    def stop(self) -> None:
        with self.lock:
            self.stopped = True
            for process in self.active:
                self.kill(process)


class Log:
    def __init__(self, directory: Path, secrets: Iterable[str] = ()) -> None:
        self.directory = directory
        self.events = (directory / "events.jsonl").open("w", encoding="utf-8")
        self.console = (directory / "console.log").open("w", encoding="utf-8")
        self.secrets = tuple(secret for secret in secrets if secret)
        self.lock = threading.Lock()
        self.indexed = 0

    def screen(self, event: str, fields: dict[str, object]) -> str:
        if event == "capture_indexed":
            self.indexed += 1
            return (
                f"[request_index] {self.indexed} captures indexed"
                if self.indexed == 1 or self.indexed % 100 == 0
                else ""
            )
        if event in {
            "model_operation",
            "provider_selected",
            "miner_started",
            "saved_response_load_started",
            "saved_response_loaded",
            "saved_response_cache_hit",
            "saved_response_cached",
            "saved_response_cache_imported",
            "saved_render_loaded",
            "request_lookup_started",
            "miner_log",
        }:
            return ""
        details = {
            key: value
            for key, value in fields.items()
            if key
            not in {
                "body",
                "source",
                "expected",
                "reproduced",
                "diff",
                "traceback",
                "rejection",
                "prompts",
                "results",
                "used_exchanges",
                "unused_exchanges",
                "image_tolerance_matches",
                "witness_images",
                "request_key",
                "recorded_request_key",
                "render_recovery",
                "render_failures",
                "missing_render_attempts",
            }
        }
        request = fields.get("recorded_request_key", fields.get("request_key"))
        if isinstance(request, dict):
            details.update(provider=request.get("provider"), request_id=request.get("request_id"))
        body = fields.get("body")
        if isinstance(body, str):
            details["body_chars"] = len(body)
        for name in ("traceback", "rejection"):
            value = fields.get(name)
            if isinstance(value, str) and value.strip():
                details["error"] = value.strip().splitlines()[-1]
        for name in ("unknown_usage_calls", "assumed_connection_failures", "assumed_render_failures", "prompts"):
            value = fields.get(name)
            if isinstance(value, list):
                details["selected_prompts" if name == "prompts" else name] = len(value)
        results = fields.get("results")
        if isinstance(results, list):
            passed = sum(bool(row.get("passed")) for row in results if isinstance(row, dict))
            details["passed_prompts"] = passed
            details["failed_prompts"] = len(results) - passed
        if "prompt_id" in details:
            details.setdefault("prompt", details.pop("prompt_id"))
        for name in ("call_id", "recorded_call_id", "exchange_id", "source_commit", "competition_ref", "png_sha256"):
            value = details.get(name)
            if isinstance(value, str):
                details[name] = value[:12]
        if event not in ("prompt_started", "prompt_finished") and isinstance(details.get("prompt"), str):
            details["prompt"] = str(details["prompt"])[:10]
        return f"[{event}] " + json.dumps(details, ensure_ascii=False)

    def emit(self, event: str, **fields: object) -> None:
        row = {"event": event, "time": time.time(), **fields}
        encoded = json.dumps(row, ensure_ascii=False)
        text_fields = {
            key: value
            for key, value in fields.items()
            if isinstance(value, str)
            and key in {"body", "source", "expected", "reproduced", "diff", "traceback", "rejection"}
        }
        display = (
            f"[{event}] "
            + json.dumps({k: v for k, v in fields.items() if k not in text_fields}, ensure_ascii=False, indent=2)
            + "\n"
        )
        for key, value in text_fields.items():
            display += f"--- {key} ---\n{value}\n"
        if event == "miner_log":
            display = f"[{fields.get('prompt')}] {fields.get('text')}"
        for secret in self.secrets:
            encoded = encoded.replace(secret, "[REDACTED]")
            display = display.replace(secret, "[REDACTED]")
        with self.lock:
            self.events.write(encoded + "\n")
            self.events.flush()
            self.console.write(display)
            self.console.flush()
            screen = self.screen(event, fields)
            if screen:
                for secret in self.secrets:
                    screen = screen.replace(secret, "[REDACTED]")
                screen = re.sub(r"data:image/[^\s\"']+", "[image data saved]", screen)
                if len(screen) > 2000:
                    screen = screen[:2000] + " … [full details saved]"
                sys.stdout.write(screen + "\n")
                sys.stdout.flush()

    def close(self) -> None:
        self.events.close()
        self.console.close()

    def exception(self, event: str, **fields: object) -> None:
        self.emit(event, traceback=traceback.format_exc(), **fields)


def execute_prompt(
    *,
    directory: Path,
    plan: dict[str, Any],
    renderer: Renderer,
    final_render: Callable[[str], bytes],
    source: ReplaySource,
    log: Log,
    timeout: float,
    processes: ReplayProcesses | None = None,
) -> dict[str, Any]:
    directory = directory.resolve()
    prompt = plan["prompt"]
    output = Path(plan.get("output", directory / "replays" / prompt))
    output.mkdir(parents=True, mode=0o700)
    failure_plan = {
        key: tuple(RenderFailure(value) for value in sequence)
        for key, sequence in plan.get("render_failures", {}).items()
    }
    missing_renders: list[MissingRenderAttempt] = []
    missing_counts: dict[str, int] = {}
    recovery_limited = False
    root = Path(__file__).resolve().parents[1]
    # Start the fixed replay entry point with the current interpreter.
    process = subprocess.Popen(  # nosec B603
        [sys.executable, "-u", str(root / "replay.py"), "--worker"],
        cwd=directory / "source",
        env={
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "HOME": str(output),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        },
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    expired = threading.Event()
    if processes is not None:
        processes.add(process)

    def remove() -> None:
        ReplayProcesses.kill(process)

    def stop() -> None:
        expired.set()
        remove()

    def diagnostics() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            log.emit("miner_log", prompt=prompt, recovery_attempt=plan.get("recovery_attempt", 1), text=line)

    thread = threading.Thread(target=diagnostics, daemon=True)
    thread.start()
    timer = threading.Timer(timeout, stop)
    timer.start()
    code = 1
    parent_error = None
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps({**plan, "directory": str(directory)}) + "\n")
        process.stdin.flush()
        for line in process.stdout:
            message = json.loads(line)
            event = message.pop("event")
            if event != "rpc":
                log.emit(event, prompt_id=prompt, recovery_attempt=plan.get("recovery_attempt", 1), **message)
                continue
            reply: dict[str, Any]
            try:
                if message["operation"] == "model":
                    capture, body = source.fetch(message["exchange_id"])
                    reply = {"capture": capture.model_dump(mode="json"), "body": base64.b64encode(body).decode()}
                elif message["operation"] == "image_index":
                    reply = {"requests": source.image_index(message["request_id"], message["session_id"])}
                elif message["operation"] == "request_lookup":
                    captures = source.lookup(RequestKey.model_validate(message["request_key"]))
                    reply = {"captures": {key: item.model_dump(mode="json") for key, item in captures.items()}}
                elif message["operation"] == "request_body":
                    body = source.request_body(RequestKey.model_validate(message["request_key"]))
                    reply = {"body": base64.b64encode(body).decode()}
                elif message["operation"] == "render":
                    try:
                        png = renderer.render(message["source"], RenderOptions(**message["options"]))
                        reply = {"png": base64.b64encode(png).decode()}
                    except RenderRejected as error:
                        reply = {"rejection": error.detail_json}
                    except MissingRenderOutcome as error:
                        occurrence = missing_counts.get(error.request_key, 0)
                        if occurrence >= MAX_MISSING_RENDER_RETRIES or len(missing_renders) >= MAX_MISSING_RENDER_CALLS:
                            recovery_limited = True
                            log.emit("render_recovery_limit", prompt=prompt, request_key=error.request_key)
                            remove()
                            break
                        sequence = failure_plan.get(error.request_key, (RenderFailure.CONNECTION,))
                        failure = sequence[min(occurrence, len(sequence) - 1)]
                        missing_renders.append(MissingRenderAttempt(error.request_key, occurrence, failure))
                        missing_counts[error.request_key] = occurrence + 1
                        reply = {
                            "unavailable": str(error),
                            "missing_render": error.request_key,
                            "occurrence": occurrence,
                            "failure_kind": failure.value,
                        }
                    except RenderUnavailable as error:
                        reply = {"unavailable": str(error)}
                elif message["operation"] == "final_render":
                    reply = {"png": base64.b64encode(final_render(message["source"])).decode()}
                else:
                    raise ValueError("Unknown replay bridge operation")
            except Exception as error:
                log.exception("evidence_unavailable", prompt=prompt)
                reply = {"error": str(error)}
            if expired.is_set():
                break
            process.stdin.write(json.dumps(reply) + "\n")
            process.stdin.flush()
        code = process.wait()
    except Exception as error:
        parent_error = str(error)
        log.exception("prompt_host_error", prompt=prompt)
    finally:
        timer.cancel()
        remove()
        process.wait()
        if processes is not None:
            processes.discard(process)
        thread.join(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    path = output / "summary.json"
    summary = json.loads(path.read_bytes()) if path.is_file() else {"passed": False, "error": "Worker did not complete"}
    summary.update(prompt=prompt, process_exit_code=code, timeout=expired.is_set())
    summary["missing_render_attempts"] = [asdict(attempt) for attempt in missing_renders]
    if recovery_limited:
        summary.update(failure_type="RenderRecoveryLimit", error="Missing-render retry limit reached")
    if processes is not None and processes.stopped:
        summary["cancelled"] = True
    if expired.is_set():
        summary.update(failure_type="TimeoutError", error=f"Replay watchdog expired after {timeout:g} seconds")
        log.emit("prompt_timeout", prompt=prompt, timeout=timeout)
    elif parent_error is not None:
        summary.update(passed=False, error=parent_error, host_error=True)
    summary["passed"] = bool(summary["passed"] and code == 0 and not expired.is_set())
    path.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def recover_prompt(
    *,
    directory: Path,
    plan: dict[str, Any],
    execute: Callable[[dict[str, Any], float], dict[str, Any]],
    log: Log,
    timeout: float,
    max_attempts: int = MAX_RECOVERY_ATTEMPTS,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + timeout
    search = RenderRecoverySearch(max_attempts)
    output = directory.resolve() / "replays" / plan["prompt"]
    trials: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    selected_output = output
    archived = False
    reason = "no_missing_renders"
    failure_plan = search.next_plan()
    while failure_plan is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 and selected is not None:
            reason = "time_limit"
            break
        number = search.attempts
        trial_output = output
        if number > 1:
            if not archived:
                original = output / "attempts/001"
                original.mkdir(parents=True)
                for artifact in tuple(output.iterdir()):
                    if artifact.name != "attempts":
                        artifact.rename(original / artifact.name)
                trials[0]["directory"] = str(original)
                selected_output = original
                archived = True
            trial_output = output / "attempts" / f"{number:03}"
            log.emit(
                "render_recovery_started",
                prompt=plan["prompt"],
                attempt=number,
                failure_kinds=sorted({kind.value for sequence in failure_plan.values() for kind in sequence}),
                render_failures=failure_plan,
                remaining_seconds=round(max(remaining, 0), 1),
            )
        result = execute(
            {**plan, "output": str(trial_output), "recovery_attempt": number, "render_failures": failure_plan},
            max(remaining, 0.001),
        )
        trials.append(
            {
                "attempt": number,
                "directory": str(trial_output),
                "render_failures": failure_plan,
                **{
                    key: result.get(key)
                    for key in (
                        "passed",
                        "output_exact",
                        "failure_type",
                        "error",
                        "seconds",
                        "request_matches",
                        "charged_calls",
                        "known_credits",
                        "missing_render_attempts",
                    )
                },
            }
        )
        if selected is None or result["passed"]:
            selected, selected_output = result, trial_output
        if result["passed"]:
            reason = "matched"
            break
        if (
            result.get("cancelled")
            or result.get("host_error")
            or result.get("failure_type")
            in {
                "EvidenceUnavailable",
                "ModuleNotFoundError",
                "ImportError",
                "SyntaxError",
            }
        ):
            selected, selected_output = result, trial_output
            reason = "validation_error"
            break
        observations = [
            MissingRenderAttempt(
                request_key=item["request_key"], occurrence=item["occurrence"], failure=RenderFailure(item["failure"])
            )
            for item in result.get("missing_render_attempts", [])
        ]
        if not observations and number == 1:
            break
        search.observe(failure_plan, observations)
        failure_plan = search.next_plan()
        reason = "attempt_limit" if search.attempts >= max_attempts else "no_more_error_sequences"
    assert selected is not None
    if archived:
        for name in ("renders", "returned.json", "expected.js", "reproduced.js", "output.diff"):
            artifact = selected_output / name
            if artifact.exists():
                (output / name).symlink_to(artifact.relative_to(output), target_is_directory=artifact.is_dir())
    selected.update(
        seconds=time.monotonic() - started,
        recovered=bool(selected["passed"] and len(trials) > 1),
        recovery_runs=len(trials),
        render_recovery={"stopped_reason": reason, "selected_directory": str(selected_output), "attempts": trials},
    )
    (output / "summary.json").write_text(json.dumps(selected, indent=2) + "\n")
    if len(trials) > 1:
        log.emit(
            "render_recovery_finished",
            prompt=plan["prompt"],
            passed=selected["passed"],
            attempts=len(trials),
            reason=reason,
        )
    return selected


def run_prompts(
    manifest: dict[str, Any],
    execute: Callable[[dict[str, Any]], dict[str, Any]],
    on_mismatch: str,
    log: Log,
    *,
    jobs: int = 1,
    cancel: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    completed: dict[int, dict[str, Any]] = {}
    prompts = manifest["prompts"]
    started = 0
    stopped = False
    interrupted = False
    pool: ThreadPoolExecutor | None = None
    pending: dict[Future[dict[str, Any]], int] = {}
    try:
        if jobs == 1:
            for index, prompt in enumerate(prompts):
                log.emit("prompt_started", prompt=prompt["id"], index=index + 1, total=len(prompts))
                started += 1
                result = execute(prompt)
                completed[index] = result
                if not result["passed"] and on_mismatch == "stop":
                    stopped = True
                    break
        else:
            pool = ThreadPoolExecutor(max_workers=jobs)
            next_index = 0
            while pending or (not stopped and next_index < len(prompts)):
                while not stopped and len(pending) < jobs and next_index < len(prompts):
                    prompt = prompts[next_index]
                    log.emit("prompt_started", prompt=prompt["id"], index=next_index + 1, total=len(prompts))
                    pending[pool.submit(execute, prompt)] = next_index
                    next_index += 1
                    started += 1
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                failed = False
                for future in sorted(done, key=pending.__getitem__):
                    index = pending.pop(future)
                    result = future.result()
                    completed[index] = result
                    failed = failed or not result["passed"]
                if failed and on_mismatch == "stop" and not stopped:
                    stopped = True
                    log.emit(
                        "replay_stopping",
                        reason="Prompt failed; no more prompts will start",
                        already_started_prompts=[prompts[index]["id"] for index in pending.values()],
                        action="Let already-started prompts finish and save results",
                    )
    except BaseException as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        if cancel is not None:
            cancel()
        for future in pending:
            future.cancel()
        raise
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
            for future, index in pending.items():
                if not future.cancelled() and future.exception() is None:
                    completed[index] = future.result()
        results = [completed[index] for index in sorted(completed)]
        summary = {
            "passed": not interrupted and len(results) == len(prompts) and all(r["passed"] for r in results),
            "selected_prompts": len(prompts),
            "started_prompts": started,
            "completed_prompts": len(results),
            "exact_passed_prompts": sum(
                bool(r["passed"] and not r.get("image_tolerance_matches") and not r.get("recovered")) for r in results
            ),
            "tolerant_passed_prompts": sum(
                bool(r["passed"] and r.get("image_tolerance_matches") and not r.get("recovered")) for r in results
            ),
            "recovered_passed_prompts": sum(bool(r["passed"] and r.get("recovered")) for r in results),
            "not_started_prompts": len(prompts) - started,
            "concurrency": jobs,
            "stopped_on_mismatch": stopped,
            "interrupted": interrupted,
            "results": results,
        }
        (log.directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    log.emit("replay_finished", **summary)
    return summary


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--hotkey", required=True, help="Full hotkey or unique prefix in the selected round")
    command.add_argument("--round", required=True)
    select = command.add_mutually_exclusive_group(required=True)
    select.add_argument("--prompt", help="Official prompt ID")
    select.add_argument("--all", action="store_true")
    command.add_argument("--refresh-index", action="store_true", help="Fetch the saved call index from R2 again")
    command.add_argument("--on-mismatch", choices=("stop", "continue"), default="stop")
    command.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Maximum concurrent prompt workers; defaults to 1. On stop, already-started prompts finish.",
    )
    command.add_argument(
        "--witness-image",
        "--renderer-id",
        dest="renderer_id",
        default=os.environ.get("REPLAY_RENDERER_ID") or os.environ.get("WITNESS_IMAGE"),
        help="Optional additional legacy Witness image to try before the known release images",
    )
    command.add_argument(
        "--competition-repo", default=os.environ.get("WITNESS_COMPETITION_REPO", "404-Repo/404-active-competition")
    )
    command.add_argument("--competition-branch", default=os.environ.get("WITNESS_COMPETITION_BRANCH", "main"))
    command.add_argument("--deployment", default=os.environ.get("WITNESS_DEPLOYMENT"))
    command.add_argument("--pricing", type=Path, default=Path(__file__).with_name("pricing.json"))
    command.add_argument("--output", type=Path, default=Path(".reports/replays") / time.strftime("%Y%m%d-%H%M%S"))
    command.add_argument(
        "--timeout", type=float, default=600, help="Total per-prompt watchdog, including recovery, in seconds"
    )
    command.add_argument(
        "--recovery-attempts",
        type=int,
        default=MAX_RECOVERY_ATTEMPTS,
        help="Maximum fresh executions per prompt for missing render recovery; 1 disables recovery",
    )
    return command


def main() -> int:
    args = parser().parse_args()
    if not args.deployment:
        raise SystemExit("Configure --deployment or WITNESS_DEPLOYMENT")
    if args.jobs < 1:
        raise SystemExit("--jobs must be at least 1")
    if args.recovery_attempts < 1:
        raise SystemExit("--recovery-attempts must be at least 1")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise SystemExit("Timeouts must be positive and finite")
    if not os.environ.get("R2_REPLAY_BUCKET"):
        raise SystemExit("Configure R2_REPLAY_BUCKET for saved model responses and previews")
    args.output = args.output.expanduser().resolve()
    os.umask(0o077)
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    secrets = [
        os.environ.get(key, "")
        for key in ("GITHUB_TOKEN", "WITNESS_GITHUB_TOKEN", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    ]
    log = Log(args.output, secrets)
    try:
        # Preparation dependencies are never imported by the miner worker.
        import boto3
        from botocore.config import Config
        from subnet_common.github import GitHubClient
        from subnet_common.witness.models import Scope
        from subnet_common.witness.state import R2StateStore

        from validation_worker.replay_evidence import (
            R2Evidence,
            SavedRenderer,
            SavedReplaySource,
            index_cache_path,
            prepare,
        )

        from .request_replay import PreparedReplay, RequestInventory

        objects_client = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT"],
            region_name="auto",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            config=Config(connect_timeout=5, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 5}),
        )
        bucket = os.environ.get("R2_BUCKET", "404-gen")
        replay_bucket = os.environ["R2_REPLAY_BUCKET"]

        def keys(prefix: str, *, bucket_name: str = bucket) -> Iterable[str]:
            for page in objects_client.get_paginator("list_objects_v2").paginate(Bucket=bucket_name, Prefix=prefix):
                for item in page.get("Contents", []):
                    yield str(item["Key"])

        objects = R2Evidence(R2StateStore(objects_client, bucket), keys, emit=log.emit)
        replay_objects = R2Evidence(
            R2StateStore(objects_client, replay_bucket, max_bytes=MAX_RESPONSE_BYTES),
            lambda prefix: keys(prefix, bucket_name=replay_bucket),
            emit=log.emit,
        )

        async def resolve() -> PreparedReplay:
            async with GitHubClient(args.competition_repo, token=secrets[0] or secrets[1] or None) as git:
                return await prepare(
                    git=git,
                    objects=objects,
                    round_name=args.round,
                    hotkey=args.hotkey,
                    deployment=args.deployment,
                    output=args.output,
                    pricing=args.pricing.read_bytes(),
                    emit=log.emit,
                    branch=args.competition_branch,
                    prompt_id=args.prompt,
                    index_cache=index_cache_directory(os.environ["R2_ENDPOINT"], bucket),
                    refresh_index=args.refresh_index,
                )

        prepared = asyncio.run(resolve())
        manifest = prepared.model_dump(mode="json")
        inventory = RequestInventory.model_validate_json((args.output / "bundle/requests.json").read_bytes())
        scope = Scope(args.deployment, prepared.round, prepared.hotkey)
        # The authoritative prefix is retained with the frozen input metadata.
        schedule_prefix = prepared.storage_prefix
        response_cache = index_cache_directory(os.environ["R2_ENDPOINT"], replay_bucket) / "responses-v1"
        pointer = index_cache_path(
            index_cache_directory(os.environ["R2_ENDPOINT"], bucket), scope, inventory.pricing_sha256, schedule_prefix
        )
        previous = Path(json.loads(pointer.read_bytes())) if pointer.is_file() else None
        processes = ReplayProcesses()

        source = SavedReplaySource(
            objects,
            replay_objects,
            scope,
            args.output / "bundle",
            inventory,
            image_cache=index_cache_directory(os.environ["R2_ENDPOINT"], replay_bucket) / "png-requests-v1",
            response_cache=response_cache,
            previous=previous,
            storage_prefix=schedule_prefix,
            emit=log.emit,
        )

        render_cache = index_cache_directory(os.environ["R2_ENDPOINT"], replay_bucket) / "renders-v1"
        renderer = SavedRenderer(
            replay_objects,
            objects,
            scope,
            args.renderer_id,
            cache=render_cache,
            artifact_cache=index_cache_directory(os.environ["R2_ENDPOINT"], bucket) / "final-renders-v1",
            storage_prefix=schedule_prefix,
            emit=log.emit,
        )
        log.emit(
            "replay_plan",
            **manifest,
            renderer_id=renderer.renderer_id,
            witness_images=list(renderer.witness_images),
            render_source="saved",
            replay_bucket=replay_bucket,
            python_executable=sys.executable,
            on_mismatch=args.on_mismatch,
            concurrency=args.jobs,
            response_cache=str(response_cache),
            render_cache=str(render_cache),
            output=str(args.output),
        )

        def execute(prompt: dict[str, Any]) -> dict[str, Any]:
            expected = json.loads((args.output / "prompts" / prompt["id"] / "expected.json").read_bytes())
            plan = {
                "prompt": prompt["id"],
                "image_sha256": prompt["sha256"],
                "seed": manifest["seed"],
                "source_commit": manifest["source_commit"],
                "renderer_id": renderer.renderer_id,
            }

            def attempt(trial_plan: dict[str, Any], remaining: float) -> dict[str, Any]:
                return execute_prompt(
                    directory=args.output,
                    plan=trial_plan,
                    renderer=renderer,
                    final_render=lambda source: renderer.final_render(
                        prompt["id"], source, expected["output"], expected["saved_result"]["views"]
                    ),
                    source=source,
                    log=log,
                    timeout=remaining,
                    processes=processes,
                )

            return recover_prompt(
                directory=args.output,
                plan=plan,
                execute=attempt,
                log=log,
                timeout=args.timeout,
                max_attempts=args.recovery_attempts,
            )

        summary = run_prompts(manifest, execute, args.on_mismatch, log, jobs=args.jobs, cancel=processes.stop)
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        return 0 if summary["passed"] else 1
    except KeyboardInterrupt:
        log.emit("interrupted")
        return 130
    except Exception:
        log.exception("replay_error")
        return 1
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())

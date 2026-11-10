"""Run submitted generate() with offline model responses and a host render bridge."""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import importlib.metadata
import importlib.util
import inspect
import json
import logging
import os
import platform
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypeVar, cast

from miner_runtime.observations import ReplayMismatch
from pydantic import TypeAdapter
from renderer_client import Renderer, RenderOptions, RenderRejected
from renderer_client.client import DEFAULT_OPTIONS
from witness_client import AsyncWitnessClient, WitnessClient
from witness_client.calls import RequestKey

from .render_recovery import RenderFailure, assumed_render_failure
from .request_replay import (
    AsyncRecordedWitnessClient,
    EvidenceUnavailable,
    ImageRequestIndex,
    RecordedWitnessClient,
    SavedCapture,
)


logger = logging.getLogger(__name__)
R = TypeVar("R", bound=Renderer | None)


class Bridge:
    def __init__(self, emit: Callable[..., None]) -> None:
        self.emit = emit
        self.lock = threading.Lock()

    def exchange(self, operation: str, **fields: object) -> dict[str, Any]:
        with self.lock:
            self.emit("rpc", operation=operation, **fields)
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("Replay host disconnected")
            reply: dict[str, Any] = json.loads(line)
            if "error" in reply:
                raise EvidenceUnavailable(reply["error"])
            return reply

    def fetch(self, exchange_id: str) -> tuple[SavedCapture, bytes]:
        reply = self.exchange("model", exchange_id=exchange_id)
        return SavedCapture.model_validate(reply["capture"]), base64.b64decode(reply["body"], validate=True)

    def image_index(self, request_id: int, session_id: str | None) -> dict[str, str | None]:
        reply = self.exchange("image_index", request_id=request_id, session_id=session_id)
        return ImageRequestIndex.model_validate(reply).requests

    def lookup(self, request: RequestKey) -> dict[str, SavedCapture]:
        reply = self.exchange("request_lookup", request_key=request.model_dump())
        return TypeAdapter(dict[str, SavedCapture]).validate_python(reply["captures"])

    def request_body(self, request: RequestKey) -> bytes:
        reply = self.exchange("request_body", request_key=request.model_dump())
        return base64.b64decode(reply["body"], validate=True)


class SavedRenderer:
    def __init__(self, bridge: Bridge, output: Path, renderer_id: str) -> None:
        self.bridge, self.output, self.renderer_id = bridge, output, renderer_id
        self.lock = threading.Lock()
        self.count = 0
        self.saved = 0
        self.assumed_failures: list[int] = []
        self.problem: Exception | None = None

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        return self._read("render", source, asdict(options))

    def final_render(self, source: str) -> bytes:
        return self._read("final_render", source, None)

    def _read(self, operation: str, source: str, options: dict[str, Any] | None) -> bytes:
        with self.lock:
            index = self.count
            self.count += 1
        root = self.output / "renders" / f"{index:03}"
        root.parent.mkdir(exist_ok=True)
        root.with_suffix(".js").write_bytes(source.encode())
        root.with_suffix(".request.json").write_text(
            json.dumps(options if options is not None else {"purpose": "final_submission"}, indent=2)
        )
        self.bridge.emit("render_request", index=index, source=source, options=options, operation=operation)
        fields: dict[str, object] = {"source": source}
        if options is not None:
            fields["options"] = options
        try:
            reply = self.bridge.exchange(operation, **fields)
        except EvidenceUnavailable as error:
            self.problem = error
            raise
        if "unavailable" in reply:
            failure = RenderFailure(reply.get("failure_kind", RenderFailure.CONNECTION))
            self.assumed_failures.append(index)
            root.with_suffix(".failure.json").write_text(
                json.dumps(
                    {
                        "error_type": "RenderUnavailable",
                        "error": reply["unavailable"],
                        "assumed": True,
                        "failure_kind": failure,
                        "request_key": reply.get("missing_render"),
                        "occurrence": reply.get("occurrence"),
                    },
                    indent=2,
                )
            )
            self.bridge.emit("assumed_render_failure", index=index, error=reply["unavailable"], failure_kind=failure)
            assumed_render_failure(failure, reply["unavailable"], source, RenderOptions(**(options or {})))
        if "rejection" in reply:
            self.saved += 1
            root.with_suffix(".rejection.json").write_text(reply["rejection"])
            self.bridge.emit("render_rejected", index=index, rejection=reply["rejection"])
            raise RenderRejected(reply["rejection"])
        png = base64.b64decode(reply["png"], validate=True)
        self.saved += 1
        root.with_suffix(".png").write_bytes(png)
        self.bridge.emit("render_response", index=index, png_sha256=hashlib.sha256(png).hexdigest(), bytes=len(png))
        return png


def invoke_generate(
    generate: (
        Callable[[bytes, int, WitnessClient, R], str | None]
        | Callable[[bytes, int, AsyncWitnessClient], Awaitable[str | None]]
    ),
    image: bytes,
    seed: int,
    witness: RecordedWitnessClient,
    renderer: R,
) -> str | None:
    """Run the submitted miner with recorded model and render outcomes."""
    witness._renderer = renderer
    if inspect.iscoroutinefunction(generate):

        async def run() -> str | None:
            entry = cast(Callable[[bytes, int, AsyncWitnessClient], Awaitable[str | None]], generate)
            return await entry(image, seed, AsyncRecordedWitnessClient(witness))

        return asyncio.run(run())
    return cast(Callable[[bytes, int, WitnessClient, R], str | None], generate)(image, seed, witness, renderer)


def replay(plan: dict[str, Any], emit: Callable[..., None]) -> int:
    directory = Path(plan["directory"])
    output = Path(plan.get("output", directory / "replays" / plan["prompt"]))
    inputs = directory / "prompts" / plan["prompt"]
    source = directory / "source"
    summary: dict[str, Any] = {
        "prompt": plan["prompt"],
        "passed": False,
        "output_exact": None,
        "historical_observations_available": False,
        "historical_renders_available": False,
        "render_source": "saved",
        "accounting_accepted": False,
    }
    witness, renderer = None, None
    started = time.monotonic()
    try:
        summary["python"] = platform.python_version()
        summary["packages"] = {
            name: importlib.metadata.version(name) for name in ("openai", "pydantic", "numpy", "pillow")
        }
        emit("runtime_environment", python=summary["python"], packages=summary["packages"])
        image = (inputs / "image.png").read_bytes()
        if hashlib.sha256(image).hexdigest() != plan["image_sha256"]:
            raise EvidenceUnavailable("Frozen prompt bytes changed")
        expected = json.loads((inputs / "expected.json").read_bytes())
        if not expected["available"]:
            raise EvidenceUnavailable("No completed independent submission: " + expected["reason"])
        bridge = Bridge(emit)
        renderer = SavedRenderer(bridge, output, plan["renderer_id"])
        witness = RecordedWitnessClient(
            directory / "bundle",
            emit,
            bridge.fetch,
            image_index=bridge.image_index,
            reference=image,
            lookup=bridge.lookup,
            request_body=bridge.request_body,
        )
        sys.path.insert(0, str(source))
        spec = importlib.util.spec_from_file_location("submitted_main", source / "main.py")
        if spec is None or spec.loader is None:
            raise ValueError("Cannot import root main.py")
        miner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(miner)
        emit("miner_started", source_commit=plan["source_commit"], seed=plan["seed"])
        result = invoke_generate(miner.generate, image, plan["seed"], witness, renderer)
        if result is not None and not isinstance(result, str):
            raise ValueError("generate() must return JavaScript or None")
        (output / "returned.json").write_text(json.dumps({"output": result}, ensure_ascii=False))
        if result is not None:
            (output / "reproduced.js").write_bytes(result.encode())
        if expected["output"] is not None:
            (output / "expected.js").write_bytes(expected["output"].encode())
        summary["output_exact"] = result == expected["output"]
        summary["output_is_none"] = result is None
        emit("output_comparison", exact=summary["output_exact"], expected=expected["output"], reproduced=result)
        if not summary["output_exact"]:
            difference = "".join(
                difflib.unified_diff(
                    (expected["output"] or "").splitlines(keepends=True),
                    (result or "").splitlines(keepends=True),
                    fromfile="saved.js",
                    tofile="reproduced.js",
                )
            )
            (output / "output.diff").write_text(difference)
            emit("output_mismatch", diff=difference)
        witness.finish()
        if renderer.problem is not None:
            raise renderer.problem
        if not summary["output_exact"]:
            raise ReplayMismatch("Replayed output differs from the independently saved result")
        if result is not None:
            summary["final_render_available"] = False
            emit("final_render_load_started")
            renderer.final_render(result)
            summary["final_render_available"] = True
        witness.budget.accept()
        summary["accounting_accepted"] = True
        summary["passed"] = summary["output_exact"]
    except BaseException as error:
        logger.exception("Prompt replay failed")
        summary.update(failure_type=type(error).__name__, error=str(error))
        emit("replay_error", error_type=type(error).__name__, error=str(error))
    if witness is not None:
        summary.update(
            request_matches={
                matching: sum(value == matching for value in witness.request_matches.values())
                for matching in ("exact", "exact_png_pixels", "image_tolerance")
            },
            image_tolerance_matches=[
                {
                    "call_id": call_id,
                    "recorded_call_id": witness.image_aliases[call_id].id,
                    "images": [asdict(difference) for difference in differences],
                }
                for call_id, differences in witness.image_tolerance_matches.items()
                if witness.request_matches.get(call_id) == "image_tolerance"
            ],
            charged_calls=len(witness.budget.charges),
            known_credits=str(sum(value for value in witness.budget.charges.values() if value is not None)),
            unknown_usage_calls=sorted(witness.unknown_usage),
            assumed_connection_failures=sorted(witness.assumed_failures),
            used_exchanges=sorted(witness.used),
            unused_exchanges=sorted(set(witness.inventory.captures) - witness.used),
        )
    if renderer is not None:
        summary["render_calls"] = renderer.count
        summary["historical_renders_available"] = renderer.saved > 0
        summary["assumed_render_failures"] = sorted(renderer.assumed_failures)
    summary["seconds"] = time.monotonic() - started
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    emit("prompt_finished", **summary)
    return 0 if summary["passed"] else 1


def main() -> int:
    os.umask(0o077)
    channel = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    # Both Python and native miner writes reach the parent's diagnostic stream.
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    lock = threading.Lock()

    def emit(event: str, **fields: object) -> None:
        with lock:
            channel.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")

    return replay(json.loads(sys.stdin.readline()), emit)


if __name__ == "__main__":
    raise SystemExit(main())

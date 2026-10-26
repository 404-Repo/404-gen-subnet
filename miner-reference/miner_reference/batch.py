"""Run independent, recoverable prompt pipelines with bounded model and render calls."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Unpack, cast

from bittensor_wallet import Wallet
from miner_runtime.observations import Inputs
from miner_runtime.runner import run_generation
from openai import APIError
from openai.types.responses import Response
from openai.types.responses.response_create_params import ResponseCreateParamsBase
from renderer_client import ArtifactClient, Renderer, RendererClient
from witness_client import WitnessClient, WitnessConfig, WitnessError
from witness_client.calls import canonical
from witness_client.state import save

from . import images, pipeline
from .pipeline import ModelCalls
from .renders import SavedRenderer
from .submissions import run_and_save


@dataclass(frozen=True)
class Prompt:
    id: str
    path: Path
    sha256: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9]{1,128}", self.id) or not re.fullmatch(r"[a-f0-9]{64}", self.sha256):
            raise ValueError("Expected an alphanumeric prompt ID and frozen image SHA256")

    def read(self) -> bytes:
        image = self.path.read_bytes()
        if hashlib.sha256(image).hexdigest() != self.sha256:
            raise ValueError(f"Frozen prompt changed: {self.id}")
        return image


def check_prompts(prompts: Sequence[Prompt]) -> None:
    if not prompts or len({p.id for p in prompts}) != len(prompts) or len({p.sha256 for p in prompts}) != len(prompts):
        raise ValueError("Each batch prompt must have a unique ID and frozen image")
    for prompt in prompts:
        prompt.read()


@dataclass(frozen=True)
class BatchSettings:
    output: Path
    seed: int
    namespace: str
    parallel_prompts: int = 4
    parallel_calls: int = 2
    parallel_renders: int = 1
    submission_commit: str | None = None

    def __post_init__(self) -> None:
        if not all(1 <= n <= 32 for n in (self.parallel_prompts, self.parallel_calls, self.parallel_renders)):
            raise ValueError("Concurrency must be between 1 and 32")
        if self.submission_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", self.submission_commit):
            raise ValueError("Expected the admitted source commit for managed submissions")
        if not 0 <= self.seed < 2**64:
            raise ValueError("Expected a uint64 generation seed")


class LimitedCalls:
    def __init__(
        self,
        calls: ModelCalls,
        directory: Path,
        progress: Callable[[str], None],
    ) -> None:
        self.calls, self.directory, self.progress = calls, directory, progress
        directory.mkdir(mode=0o700, exist_ok=True)

    def create(self, *, request_id: int = 0, **params: Unpack[ResponseCreateParamsBase]) -> Response:
        self.progress(f"call {request_id} {params.get('model')}: running")
        try:
            result = self.calls.create(request_id=request_id, **params)
        except APIError as error:
            save(self.directory / f"{request_id:02}.json",
                 canonical({"model": params.get("model"), "error": error.code, "usage": None}))
            raise
        # A fixed stage file replaces its replayed observation; totals do not grow
        # when a restart consumes the same response again.
        save(self.directory / f"{request_id:02}.response.json", result.model_dump_json().encode())
        save(
            self.directory / f"{request_id:02}.json", canonical({"model": params.get("model"), "usage": None if result.usage is None else result.usage.model_dump()})
        )
        self.progress(
            f"call {request_id}: complete, input={result.usage.input_tokens if result.usage else 'unknown'} "
            f"output={result.usage.output_tokens if result.usage else 'unknown'}"
        )
        return result


def run_batch(
    prompts: Sequence[Prompt],
    settings: BatchSettings,
    witness: WitnessClient,
    renderer: Renderer,
    *,
    progress: Callable[[str], None] = print,
) -> dict[str, str]:
    check_prompts(prompts)
    root = settings.output
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise ValueError("Batch output must be private (chmod 700)")
    identity = canonical(
        {
            "version": 2,
            **({"submission_commit": settings.submission_commit} if settings.submission_commit else {}),
            "namespace": settings.namespace,
            "seed": settings.seed,
            "renderer_id": renderer.renderer_id,
            "pipeline_sha256": hashlib.sha256(
                Path(pipeline.__file__).read_bytes()
                + Path(images.__file__).read_bytes()
                + pipeline.INSTRUCTIONS.encode()
            ).hexdigest(),
            "prompts": [{"id": p.id, "sha256": p.sha256} for p in prompts],
        }
    )
    witness._call_slots = threading.BoundedSemaphore(settings.parallel_calls)
    render_slots = threading.BoundedSemaphore(settings.parallel_renders)
    console = threading.Lock()
    started = time.monotonic()

    def notify(message: str) -> None:
        with console:
            progress(f"{time.monotonic() - started:.1f}s | {message}")

    def generate(prompt: Prompt) -> str:
        image = prompt.read()
        inputs = Inputs.supplied(image, settings.seed)
        directory = root / prompt.id
        directory.mkdir(mode=0o700, exist_ok=True)
        checkpoint, selected = directory / "result.json", directory / "selected.js"
        if checkpoint.exists():
            result = json.loads(checkpoint.read_bytes())
            if result["execution_id"] != inputs.id:
                raise ValueError("Saved generation identity differs")
            if result["status"] in ("selected", "failed"):
                if (
                    result["status"] == "selected"
                    and hashlib.sha256(selected.read_bytes()).hexdigest() != result["sha256"]
                ):
                    raise ValueError("Selected output changed")
                return str(result["status"])
        try:
            with (
                ArtifactClient(
                    "https://witness.internal",
                    renderer_id=renderer.renderer_id,
                    round=str(witness._config["round"]),
                    prompt_sha256=prompt.sha256,
                    prompt_image=image,
                    seed=settings.seed,
                    submission_commit=settings.submission_commit,
                    transport=witness.renderer_transport(),
                    timeout=240,
                )
                if settings.submission_commit is not None
                else nullcontext(renderer)
            ) as prompt_renderer:
                previews = SavedRenderer(prompt_renderer, directory / "renders", render_slots)

                def execute(image: bytes, seed: int, client: WitnessClient, renderer: SavedRenderer) -> str | None:
                    calls = LimitedCalls(
                        client.responses, directory / "calls", lambda text: notify(prompt.id + " | " + text)
                    )
                    return pipeline.run(image, seed, calls, renderer)

                if isinstance(prompt_renderer, ArtifactClient):
                    final_checkpoint = directory / "final.json"
                    run_and_save(
                        lambda: run_generation(execute, image, settings.seed, witness, previews),
                        prompt_renderer,
                        final_checkpoint,
                    )
                    source = json.loads(final_checkpoint.read_bytes())["output"]
                else:
                    source = run_generation(execute, image, settings.seed, witness, previews)
        except Exception as error:
            code = error.code if isinstance(error, WitnessError) else type(error).__name__
            save(checkpoint, canonical({"execution_id": inputs.id, "status": "pending", "error": code}))
            notify(f"{prompt.id} | pending: {code}")
            return "pending"
        if source is not None:
            save(selected, source.encode())
        status = "failed" if source is None else "selected"
        save(
            checkpoint,
            canonical(
                {
                    "execution_id": inputs.id,
                    "status": status,
                    "sha256": None if source is None else hashlib.sha256(source.encode()).hexdigest(),
                }
            ),
        )
        return status

    with (root / "run.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A batch is already running in this output directory") from None
        run_file = root / "run.json"
        if run_file.exists() and run_file.read_bytes() != identity:
            raise ValueError("Batch identity changed; preserve its original inputs and configuration")
        save(run_file, identity)
        results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=settings.parallel_prompts, thread_name_prefix="miner-prompt") as pool:
            jobs = {pool.submit(generate, prompt): prompt for prompt in prompts}
            for future in as_completed(jobs):
                prompt = jobs[future]
                try:
                    results[prompt.id] = future.result()
                except Exception as error:
                    results[prompt.id] = "pending"
                    notify(f"{prompt.id} | pending: {type(error).__name__}")
                notify(f"{len(results)}/{len(prompts)} | {prompt.id}: {results[prompt.id]}")
                save(
                    root / "progress.json",
                    canonical({"session_elapsed_seconds": time.monotonic() - started, "prompts": results}),
                )
        return {prompt.id: results[prompt.id] for prompt in prompts}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "run"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--witness-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--renderer-url")
    parser.add_argument("--submission-commit", help="Save final results on the witness Pod for this admitted commit")
    parser.add_argument("--renderer-id", required=True)
    parser.add_argument("--parallel-prompts", type=int, default=4)
    parser.add_argument("--parallel-calls", type=int, default=2)
    parser.add_argument("--parallel-renders", type=int, default=1)
    parser.add_argument("--wallet-name", default="miner")
    parser.add_argument("--wallet-hotkey", default="default")
    parser.add_argument("--wallet-path", default="~/.bittensor/wallets")
    parser.add_argument("--api", action="store_true", help="Use OPENAI_API_KEY instead of a local Codex subscription")
    args = parser.parse_args()
    if args.renderer_url is None and args.submission_commit is None:
        parser.error("--renderer-url is required without --submission-commit")
    manifest = json.loads(args.manifest.read_bytes())
    prompts = [Prompt(row["id"], Path(row["path"]), row["sha256"]) for row in manifest["prompts"]]
    check_prompts(prompts)
    config = cast(WitnessConfig, json.loads(args.witness_config.read_bytes()))
    if config.get("transport", "pods-v1") != "pods-v1" or not config.get("round"):
        parser.error("The batch runner requires a Pod gateway and a pinned round")
    namespace = hashlib.sha256(
        canonical(
            {
                "witness_config": config,
                "api": args.api,
                "wallet": [args.wallet_path, args.wallet_name, args.wallet_hotkey],
            }
        )
    ).hexdigest()
    settings = BatchSettings(
        args.output,
        manifest["seed"],
        namespace,
        args.parallel_prompts,
        args.parallel_calls,
        args.parallel_renders,
        args.submission_commit,
    )
    if args.action == "check":
        print(
            json.dumps(
                {
                    "prompts": len(prompts),
                    "successful_workflow_calls": 7 * len(prompts),
                    "maximum_workflow_calls": 12 * len(prompts),
                    "maximum_candidate_renders": 10 * len(prompts),
                    "parallel_prompts": settings.parallel_prompts,
                    "parallel_calls": settings.parallel_calls,
                    "parallel_renders": settings.parallel_renders,
                },
                indent=2,
            )
        )
        return
    hotkey = Wallet(
        name=args.wallet_name, hotkey=args.wallet_hotkey, path=str(Path(args.wallet_path).expanduser())
    ).hotkey
    settings = replace(
        settings,
        namespace=hashlib.sha256(
            canonical(
                {
                    "configuration": settings.namespace,
                    "hotkey": hotkey.ss58_address,
                }
            )
        ).hexdigest(),
    )
    with (
        WitnessClient(
            config,
            hotkey=hotkey,
            openai_api_key=os.environ["OPENAI_API_KEY"] if args.api else None,
            subscription=None if args.api else "codex",
        ) as witness,
        RendererClient(
            args.renderer_url or "https://witness.internal",
            renderer_id=args.renderer_id,
            api_key=os.environ.get("RENDERER_API_KEY"),
            timeout=240,
        ) as renderer,
    ):
        results = run_batch(prompts, settings, witness, renderer)
    if "pending" in results.values():
        raise SystemExit(1)


if __name__ == "__main__":
    main()

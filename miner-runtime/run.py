"""Run an async miner through the shared batch runner and export its saved results."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urljoin, urlsplit


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


async def run(args: argparse.Namespace) -> None:
    import httpx
    from bittensor_wallet import Wallet
    from loguru import logger
    from miner_runtime.batch import run as run_batch
    from subnet_common.competition.config import require_competition_config
    from subnet_common.competition.generations import GenerationsAdapter
    from subnet_common.competition.prompts import require_prompts
    from subnet_common.competition.schedule import require_schedule
    from subnet_common.competition.seed import require_seed_from_git
    from subnet_common.competition.state import require_state
    from subnet_common.github import GitHubClient

    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=args.workers + args.parallel_calls)
    )
    source = args.source.expanduser().resolve()
    sys.path.insert(0, str(source))
    spec = importlib.util.spec_from_file_location("main", source / "main.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load miner entrypoint: {source / 'main.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    generate = module.generate
    repository = "404-Repo/404-active-competition"
    async with GitHubClient(repository, token=os.environ.get("GITHUB_TOKEN")) as git:
        revision = await git.get_ref_sha(args.competition_branch)
        state = await require_state(git, revision)
        competition, schedule, seed, urls = await asyncio.gather(
            require_competition_config(git, revision),
            require_schedule(git, state.current_round, revision),
            require_seed_from_git(git, state.current_round, revision),
            require_prompts(git, state.current_round, revision),
        )
    origin = f"https://raw.githubusercontent.com/{repository}/{revision}/"
    prompts = {Path(unquote(urlsplit(url).path)).stem: urljoin(origin, url) for url in urls}
    round_name = f"{competition.name}-{state.current_round}"
    logger.info("Round {} | {} prompts | seed {} | commit {}", round_name, len(prompts), seed, revision)
    hotkey = Wallet(
        name=args.wallet_name,
        hotkey=args.wallet_hotkey,
        path=str(Path(args.wallet_path).expanduser()),
    ).hotkey

    with TemporaryDirectory(prefix="miner-runtime-") as temporary:
        config = Path(temporary) / "witness.json"
        config.write_text(json.dumps({
            "witness_url": args.witness_url,
            "proof_parameters": str(args.proof_parameters.expanduser().resolve()),
        }))
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as http:
            await run_batch(
                generate, prompts, seed,
                witness_config=config,
                workers=args.workers,
                parallel_calls=args.parallel_calls,
                parallel_preparations=args.parallel_preparations,
                attempts=args.attempts,
                wallet_name=args.wallet_name,
                wallet_hotkey=args.wallet_hotkey,
                wallet_path=args.wallet_path,
                api=args.api,
            )

            cdn = args.cdn.rstrip("/")
            response = await http.get(
                f"{cdn}/{schedule.witness_storage_prefix}rounds/{round_name}/{hotkey.ss58_address}/submitted.json",
                headers={"Cache-Control": "no-cache"},
            )
            response.raise_for_status()
            results = GenerationsAdapter.validate_json(response.content)
            if set(results) != set(prompts):
                raise RuntimeError("Saved index does not match the official prompts")

            for result in results.values():
                if not result.failure_reason and (result.js is None or result.views is None):
                    raise RuntimeError("Saved index contains an incomplete result")
                if result.js is not None:
                    result.js = f"{cdn}/{result.js}"
                if result.views is not None:
                    result.views = f"{cdn}/{result.views}"

            output = args.output.expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(GenerationsAdapter.dump_json(results, indent=2))
            print(f"Prepared {output}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Checkout containing async generate in main.py")
    parser.add_argument("--competition-branch", default="main", help="404-active-competition branch (default: main)")
    parser.add_argument("--output", type=Path, required=True, help="Destination for generated.json")
    parser.add_argument("--proof-parameters", type=Path, required=True, help="Directory containing proof parameters")
    parser.add_argument("--witness-url", default="https://witness.subnet404.xyz")
    parser.add_argument("--cdn", default="https://subnet404.xyz")
    parser.add_argument("--wallet-name", default="miner")
    parser.add_argument("--wallet-hotkey", default="default")
    parser.add_argument("--wallet-path", default="~/.bittensor/wallets")
    parser.add_argument("--workers", type=positive_int, default=8, help="Active prompts (default: 8)")
    parser.add_argument("--parallel-calls", type=positive_int, default=3, help="Active model captures (default: 3)")
    parser.add_argument("--parallel-preparations", type=positive_int, default=3, help="Active call preparations (default: 3)")
    parser.add_argument("--attempts", type=positive_int, default=3, help="Passes over unfinished prompts (default: 3)")
    parser.add_argument("--api", action="store_true", help="Use OPENAI_API_KEY instead of the local Codex login")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / "miner-runtime" / "src"), str(root / "shared")]
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

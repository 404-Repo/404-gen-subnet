"""Test Cloudflare → RunPod → OpenAI with a local Bittensor wallet."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from bittensor_wallet import Wallet

from witness_client import WitnessClient, WitnessConfig, WitnessError
from witness_client.types import Credentials


class Arguments(argparse.Namespace):
    wallet_name: str
    wallet_hotkey: str
    wallet_path: Path
    witness_url: str
    proof_parameters: Path
    model: str
    subscription: bool
    delivery_timeout: float
    resume: str | None


def main() -> None:
    """Send one small request, or retrieve an existing result with --resume."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet-name", required=True)
    parser.add_argument("--wallet-hotkey", required=True)
    parser.add_argument(
        "--wallet-path", type=Path, default=Path("~/.bittensor/wallets")
    )
    parser.add_argument(
        "--witness-url",
        default="https://404-witness-public-test.subnet404.workers.dev",
    )
    parser.add_argument(
        "--proof-parameters",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "witness-server/verifier/params",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--subscription",
        action="store_true",
        help="Use the local Codex login; otherwise use OPENAI_API_KEY.",
    )
    parser.add_argument(
        "--delivery-timeout",
        type=float,
        default=30,
        help="Retry window per delivery phase in seconds (default: 30).",
    )
    parser.add_argument(
        "--resume", help="Retrieve this exchange instead of generating."
    )
    args = parser.parse_args(namespace=Arguments())
    auth: Credentials
    if args.subscription:
        auth = {"subscription": "codex"}
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            parser.error(
                "Set OPENAI_API_KEY or pass --subscription for a local Codex login."
            )
        auth = {"openai_api_key": api_key}
    hotkey = Wallet(
        name=args.wallet_name,
        hotkey=args.wallet_hotkey,
        path=str(args.wallet_path.expanduser()),
    ).hotkey
    config: WitnessConfig = {
        "witness_url": args.witness_url,
        "proof_parameters": str(args.proof_parameters.expanduser().resolve()),
    }
    print(f"Witness: {args.witness_url}", flush=True)
    print(f"Hotkey: {hotkey.ss58_address}", flush=True)
    print(
        "Recovering result..." if args.resume else "Sending test request...", flush=True
    )
    started = time.monotonic()
    try:
        with WitnessClient(
            config, hotkey=hotkey, delivery_timeout=args.delivery_timeout, **auth
        ) as client:
            result = (
                client.resume(args.resume)
                if args.resume
                else client.create(
                    stream=True,
                    model=args.model,
                    input="Reply with exactly: witness test OK",
                    reasoning={"effort": "low"},
                )
            )
    except WitnessError as error:
        print(
            json.dumps(
                {
                    "error": str(error),
                    "source": error.source,
                    "http_status": error.status_code,
                    "code": error.code,
                    "exchange_id": error.exchange_id,
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(f"Completed in {time.monotonic() - started:.1f}s")
    print(result.output_text)
    print(f"Usage: {json.dumps(result.usage)}")


if __name__ == "__main__":
    main()

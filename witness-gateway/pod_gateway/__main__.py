"""Explicit commands only; imports and --help never start provider operations."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Ordinary CPU witness gateway")
    commands = parser.add_subparsers(dest="role")
    commands.add_parser("gateway", help="serve an exactly selected gateway Pod")
    commands.add_parser("worker", help="serve an operator-controlled witness Pod")
    commands.add_parser("revivor", help="supervise gateway and cleanup owned Pods")
    certificates = commands.add_parser(
        "certs", help="generate offline upstream TLS certificates"
    )
    certificates.add_argument("directory", type=Path)
    export = commands.add_parser(
        "export", help="read and verify an offline replay bundle"
    )
    for name in ("deployment", "round", "hotkey", "generation"):
        export.add_argument("--" + name, required=True)
    export.add_argument("--output", type=Path, required=True)
    root = Path(__file__).resolve().parents[2]
    export.add_argument(
        "--parameters", type=Path, default=root / "witness-server/verifier/params"
    )
    export.add_argument(
        "--verifier", type=Path, default=root / "witness-server/worker/verify.mjs"
    )
    export.add_argument(
        "--pricing",
        type=Path,
        default=Path(
            os.environ.get(
                "WITNESS_PRICING_FILE",
                str(root / "miner-reference/budgets/pricing.json"),
            )
        ),
    )
    args = parser.parse_args()
    role = args.role or os.environ.get("WITNESS_ROLE", "worker")
    if role == "certs":
        from .certs import create

        create(args.directory)
    elif role == "export":
        from .models import Scope
        from .records import CallRecords
        from .replay import export_bundle, native_verifier
        from .state import R2StateStore

        state = R2StateStore.connect(
            endpoint=os.environ["R2_ENDPOINT"],
            access_key=os.environ["R2_ACCESS_KEY_ID"],
            secret_key=os.environ["R2_SECRET_ACCESS_KEY"],
            bucket=os.environ["R2_BUCKET"],
        )
        export_bundle(
            records=CallRecords(state),
            evidence=state,
            scope=Scope(args.deployment, args.round, args.hotkey),
            generation_id=args.generation,
            output=args.output,
            proof_hash=hashlib.sha256(
                (args.parameters / "verifying.bin").read_bytes()
            ).hexdigest(),
            pricing_hash=hashlib.sha256(args.pricing.read_bytes()).hexdigest(),
            verify=native_verifier(args.verifier, args.parameters),
        )
    elif role == "worker":
        sys.path.insert(0, str(root / "witness-server/worker"))
        from pod import main as worker_main

        worker_main()
    elif role in ("gateway", "revivor"):
        from .settings import Settings

        settings = Settings(dict(os.environ))
        if role == "gateway":
            from .main import run
        else:
            from .revivor import run
        run(settings)
    else:
        parser.error("Unknown WITNESS_ROLE")


if __name__ == "__main__":
    main()

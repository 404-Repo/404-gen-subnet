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
    for name in ("deployment", "round", "hotkey"):
        export.add_argument("--" + name, required=True)
    export.add_argument("--execution", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--storage-prefix", default="")
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
        help="local pricing table override; defaults to the shared model table",
    )
    publish = commands.add_parser("publish-pricing", help="publish additions to the shared model table")
    publish.add_argument("--deployment", required=True)
    publish.add_argument("--pricing", type=Path, required=True)
    args = parser.parse_args()
    role = args.role or os.environ.get("WITNESS_ROLE", "worker")
    if role == "certs":
        from .certs import create

        create(args.directory)
    elif role in ("export", "publish-pricing"):
        from .pricing import PricingStore
        from .state import R2StateStore

        state = R2StateStore.connect(
            endpoint=os.environ["R2_ENDPOINT"],
            access_key=os.environ["R2_ACCESS_KEY_ID"],
            secret_key=os.environ["R2_SECRET_ACCESS_KEY"],
            bucket=os.environ["R2_BUCKET"],
        )
        prices = PricingStore(state, args.deployment)
        if role == "publish-pricing":
            prices.publish(args.pricing.read_bytes())
            print(f"Published model table for {args.deployment}")
            return

        from subnet_common.competition.schedule import RoundSchedule

        from .models import Scope
        from .records import CallRecords
        from .replay import export_bundle, native_verifier
        from .state import PrefixedState

        RoundSchedule.storage_prefix(args.storage_prefix)
        export_bundle(
            records=CallRecords(PrefixedState(state, args.storage_prefix)),
            evidence=state,
            scope=Scope(args.deployment, args.round, args.hotkey),
            execution=args.execution,
            output=args.output,
            proof_hash=hashlib.sha256(
                (args.parameters / "verifying.bin").read_bytes()
            ).hexdigest(),
            pricing=prices.read() if args.pricing is None else args.pricing.read_bytes(),
            verify=native_verifier(args.verifier, args.parameters),
            storage_prefix=args.storage_prefix,
        )
    elif role == "worker":
        sys.path.insert(0, str(root / "witness-server/worker"))
        from pod import main as worker_main

        worker_main()
    elif role in ("gateway", "revivor"):
        from .dependencies import create_tls_context, gateway_spec, worker_spec
        from .settings import GatewaySettings, RevivorSettings

        settings = GatewaySettings() if role == "gateway" else RevivorSettings()  # type: ignore[call-arg]
        # Both roles have always validated both Pod shapes and upstream TLS before
        # starting. Keep that startup contract while moving construction out of settings.
        worker = worker_spec(settings)
        gateway = gateway_spec(settings)
        tls = create_tls_context(settings)
        if isinstance(settings, GatewaySettings):
            from .main import run as run_gateway

            run_gateway(settings, worker, tls)
        else:
            from .revivor import run as run_revivor

            run_revivor(settings, worker, gateway)
    else:
        parser.error("Unknown WITNESS_ROLE")


if __name__ == "__main__":
    main()

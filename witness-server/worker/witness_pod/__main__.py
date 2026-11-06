"""Witness Pod service and offline replay export."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

from . import PROOF_PARAMETERS, VERIFY_SCRIPT


def main() -> None:
    parser = argparse.ArgumentParser(description="Witness Pod")
    commands = parser.add_subparsers(dest="command")
    export = commands.add_parser("export", help="read and verify an offline replay bundle")
    for name in ("deployment", "round", "hotkey"):
        export.add_argument("--" + name, required=True)
    export.add_argument("--execution", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--storage-prefix", default="")
    export.add_argument("--parameters", type=Path, default=PROOF_PARAMETERS)
    export.add_argument("--verifier", type=Path, default=VERIFY_SCRIPT)
    export.add_argument(
        "--pricing", type=Path, help="local pricing table override; defaults to the shared model table"
    )
    args = parser.parse_args()
    if args.command is None:
        from .pod import main as run_pod

        run_pod()
        return

    from subnet_common.competition.schedule import RoundSchedule
    from subnet_common.witness.models import Scope
    from subnet_common.witness.pricing import PricingStore
    from subnet_common.witness.state import PrefixedState, R2StateStore

    from .records import CallRecords
    from .replay import export_bundle, native_verifier
    from .storage import R2Store

    state = R2StateStore.connect(
        endpoint=os.environ["R2_ENDPOINT"],
        access_key=os.environ["R2_ACCESS_KEY_ID"],
        secret_key=os.environ["R2_SECRET_ACCESS_KEY"],
        bucket=os.environ["R2_BUCKET"],
    )
    prices = PricingStore(state, args.deployment)
    evidence = R2Store(bucket=os.environ["R2_REPLAY_BUCKET"])

    RoundSchedule.storage_prefix(args.storage_prefix)
    export_bundle(
        records=CallRecords(PrefixedState(state, args.storage_prefix)),
        evidence=evidence,
        scope=Scope(args.deployment, args.round, args.hotkey),
        execution=args.execution,
        output=args.output,
        proof_hash=hashlib.sha256((args.parameters / "verifying.bin").read_bytes()).hexdigest(),
        pricing=prices.read() if args.pricing is None else args.pricing.read_bytes(),
        verify=native_verifier(args.verifier, args.parameters),
        storage_prefix=args.storage_prefix,
    )


if __name__ == "__main__":
    main()

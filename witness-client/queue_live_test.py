"""One real queued generation, then recovery in a separate invocation.

Run this with the installed wheel. Only miner credentials are used here;
operator deployment and independent R2 evidence checks happen separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from importlib.metadata import version
from pathlib import Path
from typing import cast

from bittensor_wallet import Keypair

from witness_client import WitnessClient, WitnessConfig, WitnessError
from witness_client.state import save
from witness_client.tunnel import WebSocketTunnel
from witness_client.types import EarlyDisclosure, JSONObject


def run(
    config_file: Path,
    mnemonic_file: Path,
    output: Path,
    *,
    recover: bool,
    model: str,
    interrupt: bool,
) -> None:
    config: WitnessConfig = json.loads(config_file.read_text())
    key = Keypair.create_from_mnemonic(mnemonic_file.read_text().strip())
    report_file = output / "report.json"
    if not recover and report_file.exists():
        raise FileExistsError(
            "Inspect the existing attempt; do not silently start another generation"
        )
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    started = time.monotonic()
    report: JSONObject = (
        json.loads(report_file.read_text())
        if recover
        else {
            "package_version": version("404-witness-client"),
            "witness_url": config["witness_url"],
            "hotkey": key.ss58_address,
            "model": model,
        }
    )
    with WitnessClient(config, subscription="codex", hotkey=key) as client:
        if recover:
            result = client.resume(cast(str, report["exchange_id"]))
        else:
            if interrupt:
                # Simulate the miner process leaving immediately after the
                # witness has committed its key and authorized the request.
                authorize = client._authorize

                def leave(tunnel: WebSocketTunnel, disclosure: EarlyDisclosure) -> None:
                    authorize(tunnel, disclosure)
                    raise RuntimeError("Deliberate test disconnect after authorization")

                client._authorize = leave  # type: ignore[method-assign]
            save(report_file, json.dumps(report).encode())
            try:
                result = client.create(
                    stream=True,
                    model=model,
                    instructions="Follow the instruction exactly.",
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Return exactly this JavaScript line, with no Markdown: const geometry = new THREE.BoxGeometry(1, 1, 1);",
                                }
                            ],
                        }
                    ],
                    reasoning={"effort": "low"},
                )
            except WitnessError as error:
                report.update(exchange_id=error.exchange_id, error=str(error))
                save(report_file, json.dumps(report, indent=2).encode())
                if interrupt and "Deliberate test disconnect" in str(error):
                    print(
                        json.dumps(
                            {"interrupted": True, "exchange_id": error.exchange_id}
                        ),
                        flush=True,
                    )
                    return
                raise
        directory = client._state / result._exchange_id
        report.update(
            round=json.loads((directory / "session.json").read_bytes())["round"],
            exchange_id=result._exchange_id,
            output_text=result.output_text,
            usage=cast(JSONObject, dict(result.usage)),
            recovered=recover,
            seconds=round(time.monotonic() - started, 3),
            request_sha256=hashlib.sha256(
                (directory / "request.json").read_bytes()
            ).hexdigest(),
            response_sha256=hashlib.sha256(
                (directory / "response.bin").read_bytes()
            ).hexdigest(),
        )
        assert (
            result.output_text.strip()
            == "const geometry = new THREE.BoxGeometry(1, 1, 1);"
        )
        save(report_file, json.dumps(report, indent=2).encode())
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--hotkey-mnemonic-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--interrupt", action="store_true")
    parser.add_argument("--model", default="gpt-6-astra")
    args = parser.parse_args()
    run(
        args.config,
        args.hotkey_mnemonic_file,
        args.output,
        recover=args.recover,
        model=args.model,
        interrupt=args.interrupt,
    )

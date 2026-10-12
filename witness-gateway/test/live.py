"""Exercise queueing against a real RunPod acknowledgement worker, without GPT."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import cast

from bittensor_wallet import Keypair

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from client import QueueClient


def prepare(directory: Path) -> None:
    """Generate disposable identities; these are not registered on Bittensor."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    phrases = [Keypair.generate_mnemonic() for _ in range(3)]
    hotkeys = [Keypair.create_from_mnemonic(phrase).ss58_address for phrase in phrases]
    private = directory / "identities.json"
    with open(
        private, "x", opener=lambda path, flags: os.open(path, flags, 0o600)
    ) as file:
        json.dump(phrases, file)
    (directory / "public.json").write_text(
        json.dumps(
            {
                "round": 1,
                "hotkeys": hotkeys[:2],
            },
            indent=2,
        )
        + "\n"
    )
    membership(directory)


def membership(directory: Path) -> None:
    public = json.loads((directory / "public.json").read_text())
    now = int(time.time() * 1000)
    (directory / "membership.json").write_text(
        json.dumps(
            {
                "version": 1,
                "network": "finney",
                "netuid": 17,
                "block": 1,
                "fetched_at": now,
                "block_time": now,
                "hotkeys": public["hotkeys"],
            }
        )
        + "\n"
    )


def exercise(directory: Path, url: str, timeout: int) -> None:
    phrases = cast(list[str], json.loads((directory / "identities.json").read_text()))
    keys = [Keypair.create_from_mnemonic(phrase) for phrase in phrases]
    checks: dict[str, object] = {
        "url": url,
        "membership": "synthetic test identities",
        "checks": {},
    }
    outcomes = cast(dict[str, object], checks["checks"])
    started = time.monotonic()
    with QueueClient(url, keys[0]) as owner, QueueClient(
        url, keys[1]
    ) as other, QueueClient(url, keys[2]) as outsider:
        exchange_path = directory / "exchange.json"
        if exchange_path.exists():
            raise RuntimeError(
                "This fixture already attempted to queue a job; inspect it before retrying"
            )
        exchange = owner.prepare(
            "codex", deadline=int(time.time()) + min(timeout + 60, 1800)
        )
        exchange_path.write_text(json.dumps(exchange, indent=2) + "\n")
        identifier = str(exchange["exchange_id"])
        checks["exchange_id"] = identifier
        body = json.dumps(exchange, separators=(",", ":")).encode()

        def reject(
            label: str,
            client: QueueClient,
            method: str,
            path: str,
            payload: bytes,
            headers: dict[str, str],
            expected: int,
        ) -> None:
            response = client.http.request(
                method, path, content=payload, headers=headers
            )
            assert response.status_code == expected, (
                label,
                response.status_code,
                response.text,
            )
            outcomes[label] = response.status_code

        reject("unsigned", owner, "POST", "/run", body, {}, 401)
        reject(
            "unregistered",
            outsider,
            "POST",
            "/run",
            body,
            outsider.signed_request("POST", "/run", body),
            403,
        )
        forged = owner.signed_request("POST", "/run", body)
        forged["x-bittensor-signature"] = "0x" + "00" * 64
        reject("forged", owner, "POST", "/run", body, forged, 401)
        headers = owner.signed_request("POST", "/run", body)
        response = owner.http.post("/run", content=body, headers=headers)
        response.raise_for_status()
        accepted = response.json()
        (directory / "job-reply.json").write_text(json.dumps(accepted, indent=2) + "\n")
        assert accepted["queue_state"] == "queued", accepted
        outcomes["accepted"] = response.status_code
        reject("signature_replay", owner, "POST", "/run", body, headers, 401)
        assert owner.enqueue(exchange) == accepted
        outcomes["repeat_queue_request"] = "same acknowledgement"
        status_path = f"/status/{identifier}"
        reject(
            "other_miner_status",
            other,
            "GET",
            status_path,
            b"",
            other.signed_request("GET", status_path, b""),
            404,
        )
        statuses: list[str] = []
        while time.monotonic() - started < timeout:
            result = owner.status(identifier)
            assert set(result) <= {
                "round",
                "hotkey",
                "exchange_id",
                "queue_state",
                "deadline",
            }, result
            status = str(result.get("queue_state"))
            if not statuses or status != statuses[-1]:
                statuses.append(status)
                print(
                    json.dumps({"exchange_id": identifier, "status": status}),
                    flush=True,
                )
            if status in {"retired", "finished"}:
                break
            time.sleep(10)
        else:
            raise TimeoutError(
                "RunPod job did not finish; retain the exchange for investigation"
            )
        checks.update(
            {"statuses": statuses, "seconds": round(time.monotonic() - started, 3)}
        )
        (directory / "report.json").write_text(json.dumps(checks, indent=2) + "\n")
        assert statuses[-1] == "retired", statuses
        print(json.dumps(checks, indent=2), flush=True)


def resume(directory: Path, url: str) -> None:
    """Use after restarting the gateway with its existing durable records."""
    phrases = cast(list[str], json.loads((directory / "identities.json").read_text()))
    exchange = cast(
        dict[str, object], json.loads((directory / "exchange.json").read_text())
    )
    expected = json.loads((directory / "job-reply.json").read_text())
    with QueueClient(url, Keypair.create_from_mnemonic(phrases[0])) as client:
        repeated = client.enqueue(exchange)
        assert repeated["exchange_id"] == expected["exchange_id"]
        assert repeated["queue_state"] == "retired"
        result = client.status(str(exchange["exchange_id"]))
        assert result["queue_state"] == "retired", result
        (directory / "resumed.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "membership", "run", "resume"])
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--url")
    parser.add_argument("--timeout", type=int, default=480)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.directory)
    elif args.command == "membership":
        membership(args.directory)
    else:
        if not args.url:
            parser.error("run requires --url")
        if args.command == "resume":
            resume(args.directory, args.url)
        else:
            exercise(args.directory, args.url, args.timeout)


if __name__ == "__main__":
    main()

"""Hotkey-authenticated client for gateway queue diagnostics."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Self, cast
from urllib.parse import urlsplit
import uuid

import httpx
from bittensor_wallet import Keypair, Wallet

# Reuse the current queue job signature, without changing the released client.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
from identity import WITNESS_ID, Provider, signed_exchange


class QueueClient:
    def __init__(self, url: str, hotkey: Keypair) -> None:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"))) or parsed.username or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("Supply an HTTPS origin (HTTP allowed only for local tests)")
        if hotkey.crypto_type != 1:
            raise ValueError("Expected an sr25519 Bittensor hotkey")
        self.hotkey = hotkey
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=20, follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def prepare(self, provider: Provider, *, deadline: int | None = None) -> dict[str, object]:
        """Keep this object when retrying a queue request; never allocate a new ID."""
        return signed_exchange(self.hotkey, str(uuid.uuid4()),
                               deadline if deadline is not None else int(time.time()) + 600, provider=provider)

    def signed_request(self, method: str, path: str, body: bytes) -> dict[str, str]:
        nonce = str(time.time_ns())
        payload = "\n".join(["btauth/1", "sr25519", method, path, hashlib.sha256(body).hexdigest(),
                             nonce, self.hotkey.ss58_address, WITNESS_ID])
        return {"content-type": "application/json", "x-bittensor-version": "1",
                "x-bittensor-hotkey": self.hotkey.ss58_address, "x-bittensor-receiver": WITNESS_ID,
                "x-bittensor-nonce": nonce, "x-bittensor-signature": "0x" + self.hotkey.sign(payload.encode()).hex()}

    def _request(self, method: str, path: str, data: dict[str, object] | None = None) -> dict[str, object]:
        body = json.dumps(data, separators=(",", ":")).encode() if data is not None else b""
        response = self.http.request(method, path, content=body, headers=self.signed_request(method, path, body))
        response.raise_for_status()
        return cast(dict[str, object], response.json())

    def enqueue(self, exchange: dict[str, object]) -> dict[str, object]:
        return self._request("POST", "/run", exchange)

    def status(self, exchange_id: str) -> dict[str, object]:
        return self._request("GET", f"/status/{uuid.UUID(exchange_id)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--provider", choices=("api", "codex"), help="Required when queueing a new job")
    parser.add_argument("--wallet-name", required=True)
    parser.add_argument("--wallet-hotkey", required=True)
    parser.add_argument("--wallet-path", default="~/.bittensor/wallets")
    parser.add_argument("--status", help="Check an existing exchange instead of queueing a job")
    args = parser.parse_args()
    if not args.status and args.provider is None:
        parser.error("--provider is required for a new job")
    key = Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey, path=args.wallet_path).hotkey
    with QueueClient(args.url, key) as client:
        if args.status:
            print(json.dumps(client.status(args.status), indent=2))
        else:
            exchange = client.prepare(args.provider)
            print(f"Control/recovery ID: {exchange['exchange_id']}", flush=True)
            print(json.dumps(client.enqueue(exchange), indent=2))


if __name__ == "__main__":
    main()

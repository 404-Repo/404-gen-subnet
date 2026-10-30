"""Signed gateway requests and local subscription credentials."""

import base64
import hashlib
import json
import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import BinaryIO, TypedDict, cast

from bittensor_wallet import Keypair

from .timing import measure, timed
from .types import JSONObject, JSONValue

WITNESS_ID = "404-witness"


class CodexTokens(TypedDict):
    access_token: str
    account_id: str


class CodexLogin(TypedDict):
    auth_mode: str
    tokens: CodexTokens


class CodexAccount(TypedDict, total=False):
    type: str


class AccountResult(TypedDict, total=False):
    account: CodexAccount | None


class RpcResponse(TypedDict, total=False):
    id: int
    error: JSONValue
    result: AccountResult


def hotkey_headers(
    key: Keypair, body: bytes, *, method: str, path: str
) -> dict[str, str]:
    if key.crypto_type != 1:
        raise ValueError("Expected an sr25519 Bittensor hotkey")
    nonce = str(time.time_ns())
    payload = "\n".join(
        [
            "btauth/1",
            "sr25519",
            method,
            path,
            hashlib.sha256(body).hexdigest(),
            nonce,
            key.ss58_address,
            WITNESS_ID,
        ]
    )
    return {
        "x-bittensor-version": "1",
        "x-bittensor-hotkey": key.ss58_address,
        "x-bittensor-receiver": WITNESS_ID,
        "x-bittensor-nonce": nonce,
        "x-bittensor-signature": "0x" + key.sign(payload.encode()).hex(),
    }


@timed("subscription_credentials")
def subscription_credentials(
    home: str | os.PathLike[str] | None = None, executable: str = "codex"
) -> tuple[str, str]:
    home_path = Path(home or os.environ.get("CODEX_HOME", "~/.codex")).expanduser()

    def read() -> tuple[str, str, float]:
        data: CodexLogin = json.loads((home_path / "auth.json").read_text())
        if data.get("auth_mode") != "chatgpt":
            raise ValueError("Run Codex login with your ChatGPT subscription first")
        token = data["tokens"]["access_token"]
        payload = token.split(".")[1]
        expiry: float = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )["exp"]
        return token, data["tokens"]["account_id"], expiry

    with measure("credential_load"):
        token, account, expiry = read()
    if expiry < time.time() + 300:
        with measure("credential_refresh"):
            _refresh_codex(home_path, executable)
        token, account, expiry = read()
    if expiry < time.time() + 300:
        raise ValueError(
            "Subscription login expires too soon; sign in again with Codex"
        )
    return token, account


def _refresh_codex(home: Path, executable: str) -> None:
    # Codex owns subscription refresh. This is an auth helper only; inference
    # and proof generation never launch Codex, Node, or the Go CLI.
    process = subprocess.Popen(
        [
            executable,
            "app-server",
            "--stdio",
            "-c",
            'cli_auth_credentials_store="file"',
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "CODEX_HOME": str(home)},
    )
    stdin = cast(BinaryIO, process.stdin)
    stdout = cast(BinaryIO, process.stdout)

    def send(value: JSONObject) -> None:
        stdin.write(json.dumps(value).encode() + b"\n")
        stdin.flush()

    pending = b""
    deadline = time.monotonic() + 30
    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "witness_client_python", "version": "0.1.0"}
                },
            }
        )
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                data = os.read(stdout.fileno(), 65536)
                if not data:
                    break
                pending += data
                if len(pending) > 1024 * 1024:
                    break
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    message: RpcResponse = json.loads(line)
                    if message.get("error"):
                        raise RuntimeError("Local Codex login refresh failed")
                    if message.get("id") == 1:
                        send({"method": "initialized"})
                        send(
                            {
                                "id": 2,
                                "method": "account/read",
                                "params": {"refreshToken": True},
                            }
                        )
                    elif message.get("id") == 2:
                        if (message.get("result", {}).get("account") or {}).get(
                            "type"
                        ) != "chatgpt":
                            raise RuntimeError(
                                "A local ChatGPT subscription login is required"
                            )
                        return
        raise RuntimeError("Local Codex login refresh did not finish")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        stdin.close()
        stdout.close()

"""Generate three.js source from image URLs, using one witnessed call per image.

Rendering is your responsibility. Rendering and output requirements will be
published separately. Each 3D model has a shared budget of at most 60 OpenAI
calls and less than $5 of API-equivalent token usage, across all its attempts.
See the README for accounting rules and the public playground allowance.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from bittensor_wallet import Wallet

from witness_client import WitnessClient, WitnessError
from witness_client.types import Credentials


class MinerArguments(argparse.Namespace):
    image_urls: list[str]
    config: str
    model: str
    subscription: bool
    output: Path
    wallet_name: str
    wallet_hotkey: str
    wallet_path: str


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("image_urls", nargs="+")
parser.add_argument("--config", default="witness.json")
parser.add_argument("--model", required=True)
parser.add_argument("--subscription", action="store_true")
parser.add_argument("--output", type=Path, default=Path("results"))
parser.add_argument("--wallet-name", default="default")
parser.add_argument("--wallet-hotkey", default="default")
parser.add_argument("--wallet-path", default="~/.bittensor/wallets")
args = parser.parse_args(namespace=MinerArguments())
args.output.mkdir(parents=True, exist_ok=True)
hotkey = Wallet(
    name=args.wallet_name, hotkey=args.wallet_hotkey, path=args.wallet_path
).hotkey

auth: Credentials = (
    {"subscription": "codex"}
    if args.subscription
    else {"openai_api_key": os.environ["OPENAI_API_KEY"]}
)
with WitnessClient(args.config, hotkey=hotkey, **auth) as client:
    for number, image_url in enumerate(args.image_urls, start=1):
        try:
            result = client.responses.create(
                stream=True,
                model=args.model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Give me 3D three.js for this image. Return only JavaScript, without Markdown fences.",
                            },
                            {
                                "type": "input_image",
                                "image_url": image_url,
                                "detail": "auto",
                            },
                        ],
                    }
                ],
            )
        except WitnessError as error:
            print(
                json.dumps(
                    {
                        "error": str(error),
                        "source": error.source,
                        "http_status": error.status_code,
                        "code": error.code,
                        "provider_message": error.provider_message,
                        "provider_code": error.provider_code,
                        "param": error.param,
                        "exchange_id": error.exchange_id,
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        path = args.output / f"{number:03}.js"
        path.write_text(result.output_text, encoding="utf-8")
        submission = {
            "hotkey": result.hotkey,
            "image_url": image_url,
            "result": path.name,
            "usage": result.usage,
        }
        (args.output / f"{number:03}.json").write_text(
            json.dumps(submission, indent=2), encoding="utf-8"
        )
        print(f"{image_url} -> {path} ({result.hotkey})")
        print(f"Usage: {json.dumps(result.usage)}")

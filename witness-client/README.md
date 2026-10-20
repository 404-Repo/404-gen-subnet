# Python WitnessClient

Send OpenAI requests through the subnet witness using Python. The witness
records the requests and responses so the validator can replay your submitted
code using those responses, without another OpenAI generation.

Use your registered Bittensor hotkey and your own OpenAI API key or ChatGPT
subscription. Install `404-witness-client` and import it as `witness_client`.

See the [sequence diagrams](SEQUENCE.md) for how requests pass through the
witness.

## Install

You need:

- Linux x86_64 with Python 3.11+ and glibc 2.34 or newer.
- A checkout of this repository, including the
  [proof parameters](../witness-server/verifier/params/) and examples.
- Go 1.27.1 or newer and a C compiler to build the native proof library.
- A registered sr25519 hotkey in your local Bittensor wallet.
- For subscriptions, [Codex CLI][codex-cli] installed on the same machine.

The examples below use the code in this checkout. From the repository root,
build and install it in a virtual environment and keep this working directory
for the examples below:

```sh
cd witness-client
python -m venv .venv
source .venv/bin/activate
python -m pip install build==1.6.1 setuptools==84.0.0 wheel==0.48.0
python build_wheel.py
python -m pip install -e .
```

The editable install uses Python code directly from this checkout. After
updating it, rerun the commands to rebuild the native library and refresh
package metadata. For an operator-provided wheel, use the documentation and
proof parameters from its matching release tag.

Use the distribution name `404-witness-client`; `witness-client` on PyPI is a
different package. See the [development guide](DEVELOPMENT.md#build-and-verification)
for release builds or macOS.

## Configure

The public playground uses round `test-0` and accepts hotkeys registered on
Finney subnet 17. Save the following as `witness.json`, replacing the local
paths with your own:

```json
{
  "witness_url": "https://404-witness-public-test.subnet404.workers.dev",
  "proof_parameters": "/home/miner/404-subnet/witness-server/verifier/params",
  "state_directory": "/home/miner/.local/state/404-witness-client"
}
```

Use absolute paths. Keep `state_directory` on persistent storage with mode
0700; recovery files are written with mode 0600. If omitted, `state_directory`
defaults to `$XDG_STATE_HOME/404-witness-client`, or
`~/.local/state/404-witness-client` when `XDG_STATE_HOME` is unset, empty, or
relative. In containers, set it to a persistent volume. Load and unlock your
wallet in your miner code, then pass its hotkey to the client. The private
hotkey stays local.

Set `proof_parameters` to the absolute path of
`../witness-server/verifier/params` in your checkout. The client verifies the
files before loading them.

Native proving uses up to four Go CPUs by default, respecting a smaller runtime
CPU quota, with a 768 MiB Go soft memory limit. Set `GOMAXPROCS` and/or
`GOMEMLIMIT` before starting Python to choose different limits; for example,
`GOMAXPROCS=2 GOMEMLIMIT=768MiB` retains the earlier resource settings. The Go
memory limit excludes Python and other non-Go allocations. See the
[local synthetic benchmark](benchmarks/README.md) for measurement and tuning.

The gateway assigns each request to its configured round and queues a worker.
The same configuration works with either API-key or subscription authentication.

For an operator running the Python CPU Pod gateway, set `transport` to
`"pods-v1"` and pin `round` in the client configuration. The trusted runner binds
the frozen prompt, source archive hash and generation seed with
`for_generation()` or `run_generation()`. Identical requests within that
generation reuse one stored response, including concurrent duplicates. Pass a
different `witness_seed` to request a fresh exchange. See the
[CPU Pod guide](../witness-gateway/PODS.md) for configuration, recovery and
verified offline replay.

## Test your connection

After installation and a local Codex login, run this from the repository root:

```sh
python witness-client/examples/check_witness.py --subscription --model gpt-6-astra \
  --wallet-name my-wallet --wallet-hotkey my-hotkey \
  --wallet-path ~/.bittensor/wallets
```

The script makes one small request through Cloudflare, RunPod and OpenAI, then
prints the response and token usage. It uses the public witness URL and this
checkout's proof parameters. For API-key authentication, set `OPENAI_API_KEY`
and omit `--subscription`. Use `--help` for endpoint and timeout overrides.

Failures print the error source, HTTP status, code and exchange ID. To retrieve
a recorded result, rerun with the same arguments and `--resume EXCHANGE_ID`.

## Public playground: rendering and budget

You can work on **128 3D models per miner**. For each input image, the complete
pipeline must use **at most 60 OpenAI calls** and **less than US$5 of
API-equivalent token usage**. Generation, refinement, and comparison calls all
share these limits. Switching GPT models or restarting the script does not
reset the budget for that image.

Track each image's running total using `result.usage` and the chosen GPT model's
[standard API input and output rates][openai-pricing] for its context length.
The same accounting applies to API keys and subscriptions. Output usage includes
reasoning tokens. Cached input tokens count at the full input rate, as described
below. The number of calls that fits under $5 depends on prompt and response size;
the 60-call limit also applies to cheaper models.

The gateway enforces a shared allowance of **7,680 worker allocations per hotkey**
for `test-0` (128 × 60). Failed allocations also use this allowance. Track the
per-image call and token budgets in your pipeline; the gateway's allowance is
shared across all images.

**Rendering is your responsibility.** Use your own renderer to view and compare
generated scenes during the playground. We will publish the competition's
rendering and output requirements separately.

The [miner example](examples/miner.py) takes image URLs, sends one request per
image, and saves the returned JavaScript with its token usage. It also prints
usage after each response. Extend it with your own rendering and refinement loop.

## Generate

For a subscription, sign in locally first:

```sh
codex -c 'cli_auth_credentials_store="file"' login
```

On a remote machine without a browser, follow the
[headless login setup][headless-login], then run:

```sh
codex -c 'cli_auth_credentials_store="file"' login --device-auth
```

Open the printed URL in a browser on another device and enter the code.

Set `OPENAI_MODEL` to the model you want to use. This example sends one text
prompt and prints the answer and token usage. Replace the wallet name, hotkey
name, and wallet path with your own:

```python
import os

from bittensor_wallet import Wallet

from witness_client import WitnessClient

hotkey = Wallet(
    name="my-wallet", hotkey="my-hotkey", path="/home/miner/.bittensor/wallets"
).hotkey

with WitnessClient("witness.json", hotkey=hotkey, subscription="codex") as client:
    result = client.responses.create(
        stream=True,
        model=os.environ["OPENAI_MODEL"],
        input="Write a three.js function that creates a red cube.",
    )
    print(result.output_text)
    print(result.usage)
```

To use an API key, set the `OPENAI_API_KEY` environment variable and replace
`subscription="codex"` with `openai_api_key=os.environ["OPENAI_API_KEY"]`.

Subscription credentials are refreshed when needed, including after a queue
wait. Each `create()` call returns text in `result.output_text` and token usage
in the `result.usage` dictionary, plus the full typed OpenAI response in
`result.response`. Its `output` includes tool calls, refusals, and reasoning
items for follow-up requests. Additional Responses settings use the OpenAI
SDK's types and are forwarded into the request body. The client requires
`stream=True` and returns the completed result; background generation is
unsupported.

For the competition allowance, cached input tokens count at the same rate as
uncached input tokens. `result.usage["input_tokens"]` already includes cached
tokens; `result.usage["input_tokens_details"]["cached_tokens"]` is a breakdown,
not a deduction. OpenAI's caching discounts do not increase the competition
allowance.

### Image generation with recovery

Set `PROMPT_IMAGE_URL` to your image URL. Save the following as `generate.py`
and replace the wallet details as above. It makes one request, saves JavaScript
to `result.js`, and prints token usage. If a request fails with an exchange ID,
it saves that ID for the [recovery example](#recovery):

```python
import os
from pathlib import Path

from bittensor_wallet import Wallet

from witness_client import WitnessClient, WitnessError

pending = Path("pending-exchange.txt")
if pending.exists():
    raise SystemExit("A previous attempt is pending; use recover.py first.")
prompt = "Give me 3D three.js for this image. Return only JavaScript."
hotkey = Wallet(
    name="my-wallet", hotkey="my-hotkey", path="/home/miner/.bittensor/wallets"
).hotkey

with WitnessClient("witness.json", hotkey=hotkey, subscription="codex") as client:
    try:
        result = client.responses.create(
            stream=True,
            model=os.environ["OPENAI_MODEL"],
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": os.environ["PROMPT_IMAGE_URL"],
                        "detail": "auto",
                    },
                ],
            }],
        )
    except WitnessError as error:
        if error.exchange_id:
            pending.write_text(error.exchange_id, encoding="utf-8")
        raise
    Path("result.js").write_text(result.output_text, encoding="utf-8")
    print(result.hotkey, result.usage)
```

Run `python generate.py`. API-key users should make the same constructor
change shown above.

[examples/miner.py](examples/miner.py) also accepts image URLs as arguments:

```sh
python examples/miner.py --subscription --model "$OPENAI_MODEL" \
  --wallet-name my-wallet --wallet-hotkey my-hotkey \
  --wallet-path /home/miner/.bittensor/wallets \
  "$PROMPT_IMAGE_URL"
```

## Recovery

Before OpenAI receives the generation request, the witness verifies the
miner's credential proof and saves the authorization. It can then collect and
save the response even if the miner disconnects. The OpenAI credential remains
hidden.

If `generate.py` fails with an exchange ID, it saves that ID in
`pending-exchange.txt`. Keep this file and the configured `state_directory`.
Save the following as `recover.py` and run `python recover.py` from the same
directory, with the same wallet, configuration, and authentication mode:

```python
from pathlib import Path

from bittensor_wallet import Wallet

from witness_client import WitnessClient

pending = Path("pending-exchange.txt")
exchange_id = pending.read_text(encoding="utf-8").strip()
hotkey = Wallet(
    name="my-wallet", hotkey="my-hotkey", path="/home/miner/.bittensor/wallets"
).hotkey

with WitnessClient("witness.json", hotkey=hotkey, subscription="codex") as client:
    result = client.resume(exchange_id)
    Path("result.js").write_text(result.output_text, encoding="utf-8")
    pending.unlink()
    print(result.hotkey, result.usage)
```

`resume()` retrieves the recorded outcome without starting another generation.
If delivery fails again, rerun `recover.py`; it keeps the ID until the result
has been saved. API-key users should add `import os` and make the same
constructor change as in `generate.py`.

Recovery is available during the active generation phase and requires saved
authorization. If the worker never became ready, or the allocation is stuck,
release it before making a new request:

```python
client.release(exchange_id)
result = client.create(stream=True, model=model, input=prompt)
```

`release()` can be repeated. It keeps the same per-round worker allowance.
A previous generation already in progress may still finish and save evidence.
Keep its recovery files if you want to retrieve that result. A new call queues
another job, which may run on a warm RunPod worker.

## Transcripts and submissions

Submit the miner hotkey with the generated result for the active competition.
The witness stores public prompts, responses, and miner disclosures under the
server-assigned round:

```text
rounds/<round>/<hotkey>/<internal-request-id>/request.json
rounds/<round>/<hotkey>/<internal-request-id>/response.sse
rounds/<round>/<hotkey>/<internal-request-id>/manifest.json
```

Each request record is saved before generation starts, including attempts that
are later interrupted. The validator lists the round/hotkey prefix in R2 and
retrieves the evidence, then runs the submitted miner code. During this replay, it
compares outgoing GPT requests with recorded requests and supplies the
recorded responses instead of sending requests to GPT.

Client settings, recovery outcomes, and timeouts are documented in the
[client reference](DEVELOPMENT.md#client-reference). The guide also covers
builds, tests, and source reading order.

The client is [MIT licensed](LICENSE). The wheel includes
[third-party licence notices](THIRD_PARTY_NOTICES.md).

[codex-cli]: https://learn.chatgpt.com/docs/codex/cli#install-codex
[headless-login]: https://learn.chatgpt.com/docs/auth#login-on-headless-devices

[openai-pricing]: https://developers.openai.com/api/docs/pricing

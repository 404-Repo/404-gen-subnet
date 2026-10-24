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
  [proof parameters](../witness-server/verifier/params/).
- Go 1.27.1 or newer and a C compiler to build the native proof library.
- A registered sr25519 hotkey in your local Bittensor wallet.
- For subscriptions, [Codex CLI][codex-cli] installed on the same machine.

The commands below use the code in this checkout. From the repository root,
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

Use the HTTPS gateway origin and pinned competition round supplied by the
operator. Save the configuration as `witness.json`, replacing the paths:

```json
{
  "transport": "pods-v1",
  "round": "test-0",
  "witness_url": "https://witness.example.org",
  "proof_parameters": "/home/miner/404-subnet/witness-server/verifier/params",
  "state_directory": "/home/miner/.local/state/404-witness-client"
}
```

`pods-v1` is the default transport. A pinned `round` is required. The same
configuration works with an OpenAI API key or a ChatGPT subscription.

Use absolute paths. Keep `state_directory` on persistent storage with mode 0700;
recovery files use mode 0600. The default directory is
`$XDG_STATE_HOME/404-witness-client`, or `~/.local/state/404-witness-client` when
that variable is unset, empty or relative. The client checks the proof parameter
files before loading them. Load and unlock your wallet in the trusted runner and
pass its sr25519 hotkey to the client. The private hotkey stays local.

Native proving uses up to four Go CPUs by default, respecting a smaller runtime
CPU quota, with a 768 MiB Go soft memory limit. Set `GOMAXPROCS` and `GOMEMLIMIT`
before starting Python to choose different limits. The Go memory limit excludes
Python and other non-Go allocations. See the
[local synthetic benchmark](benchmarks/README.md) for measurement and tuning.

## Generate

The trusted runner supplies miner code with `generate(image, seed, witness,
renderer)`. Its `witness` is already bound to the frozen image and generation seed. See the [miner reference](../miner-reference/README.md) for
the submission interface, batch runner, rendering and budgets.

Inside `generate()`, use the supplied client:

```python
result = witness.responses.create(
    stream=True,
    model=model,
    input=prompt,
    instructions="Return JavaScript.",
)
source = result.output_text
```

Streaming is enabled internally. The client returns a completed `WitnessResult`.
`result.response` preserves the typed OpenAI response, including tools, refusals
and reasoning. `result.usage` contains token counts. Additional Responses
settings use the OpenAI SDK's types. Background generation is unsupported.

The trusted runner creates the bound client with `for_generation()` or wraps the
function using `run_generation()`:

```python
from witness_client import WitnessClient, run_generation

with WitnessClient("witness.json", hotkey=hotkey, subscription="codex") as client:
    source = run_generation(generate, frozen_png, seed, client, renderer)
```

The trusted runner binds the frozen prompt and generation seed outside miner
execution. A direct `client.create()` requires a generation binding.
To use an API key, replace `subscription="codex"` with
`openai_api_key=os.environ["OPENAI_API_KEY"]`.

Subscription users sign in locally through Codex with file credential storage.
For a machine without a browser, use the supported device-login flow. The client
refreshes the local login when needed, including after waiting for a worker.
Provider credentials stay hidden by the credential proof.

Identical requests within a generation share a recorded response, including
concurrent duplicates. A different `request_id` requests a fresh exchange for
otherwise identical input. Restarting the same generation consumes the saved
prefix before appending new work; changed requests in that prefix are rejected.

## Concurrency

`WitnessClient(..., parallel_preparations=3)` bounds simultaneous call
preparations across generations. The default is three and the range is 1 through
32. A slot covers dispatch, TLS, credential proving and durable authorization;
it becomes available while the provider runs. Native proving remains serialized.

The trusted runner separately bounds active captures with a shared `call_slots`
semaphore passed to `for_generation()`. The batch runner sets this from
`--parallel-calls`. For three preparations and twelve captures, use the default
client setting and `--parallel-calls 12` with a Pod sized for twelve captures.
The capture limit includes preparation. Saved-response and recorded-order waits
do not occupy either limit.

## Recovery and replay

Before the provider receives billable request bytes, the worker verifies the
credential proof and saves authorization. It then captures and persists the
response independently of the live client connection. Delivery waits and upload
retries reuse saved data.

Keep the original configuration, state directory, hotkey and authentication mode.
A `WitnessError.exchange_id` identifies the attempt. Retrieve it with
`client.resume(exchange_id)` during the active generation phase. A repeated
`run_generation()` also resumes the existing generation prefix. Do not change
its frozen inputs or identity to recover it.

`client.release(exchange_id)` releases a stuck capture without resetting usage.
A request already running may still finish and save evidence. Preserve its
recovery files and unresolved accounting. Releasing does not start another
provider request.

The validator exports and native-verifies recordings, then supplies retained
responses during replay. It checks invocation order, delivery order and output
bytes. The [Pod guide](../witness-gateway/PODS.md) describes verified offline
replay, storage layout and lifecycle.

## Usage and evidence

The gateway enforces the operator's frozen per-prompt allowance across generation
seeds, source revisions, credentials and Pod replacements. All input tokens count
at their full configured weight, including cached input; output includes reasoning
once. Possible dispatches consume call allowance, and unknown usage stays pending.
Provider rejections and interrupted attempts retain their evidence.

Private evidence is grouped under `rounds/<round>/<hotkey>/`, including per-exchange
requests, responses and manifests and generation recordings. Submit the generated
result and miner hotkey according to the competition's runner contract.

See the [client reference](DEVELOPMENT.md#client-reference) for settings, recovery
and timeouts, and [TIMINGS.md](TIMINGS.md) for diagnostics. The client is
[MIT licensed](LICENSE); the wheel includes
[third-party licence notices](THIRD_PARTY_NOTICES.md).

[codex-cli]: https://learn.chatgpt.com/docs/codex/cli#install-codex

For subscription requests, an optional `session_id` sends a nonsecret routing
header across related calls. It is separate from `request_id` and
`prompt_cache_key`; it does not change recorded request identity.
`ExchangePending` reports an unresolved capture that should be resumed without
starting another inference.

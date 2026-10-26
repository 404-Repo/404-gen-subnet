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
- Go 1.27.3 or newer and a C compiler to build the native proof library.
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

Use the HTTPS gateway origin supplied by the operator. Save the configuration
as `witness.json`, replacing the paths:

```json
{
  "witness_url": "https://witness.example.org",
  "proof_parameters": "/home/miner/404-subnet/witness-server/verifier/params",
  "state_directory": "/home/miner/.local/state/404-witness-client"
}
```

The gateway selects its current competition round and returns its metadata.
The same configuration works across round changes and with an OpenAI API key or
a ChatGPT subscription. Final checkpoint reuse checks the prompt hash, seed,
renderer identity and approved source commit read from the authenticated Pod.

The gateway authenticates registration and returns the assigned Pod's public IP,
mapped TCP port, verified TLS name and scoped access grant. Calls, proofs,
call state and rendering use that Pod directly. An active assignment is
reused across transport reconnects and client restarts. Gateway control traffic
does not carry generation payloads.

The first operation waits for the assigned Pod to become ready. Subsequent
operations use that Pod directly; renewing its access grant keeps the same Pod.
If startup exceeds `delivery_timeout`, retrying the connection resumes the saved
allocation. The client retains its allocation ID across restarts and network
failures. A missing cached assignment does not request another Pod. A confirmed
terminal allocation permits a replacement.

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

The runtime supplies `generate(image, seed, witness, renderer)` with frozen inputs
and clients. Model calls work without prompt binding or recording lifecycle APIs:

```python
response = witness.responses.create(model=model, input=prompt, instructions="Return JavaScript.")
source = response.output_text
```

The default result is an OpenAI `Response`, including output items and typed usage.
Failed/incomplete Responses retain their status. Provider HTTP failures use SDK
status exceptions. `stream=True` returns typed SSE events with close/context-manager
support, buffered until the capture is sealed; it does not expose token timing.
Background generation and raw SDK transport options are unsupported. Infrastructure
failures use `WitnessError`/`ExchangePending` and never automatically repeat an
ambiguously dispatched paid request.

A direct `WitnessClient.responses.create()` also works for other work during the
round's access window. Equal provider/body/`request_id`/subscription-`session_id`
keys reuse a saved outcome throughout the miner's allocation scope. Use a stable,
different `request_id` for intentionally fresh inference. It is distinct from the
generation seed and is not sent to OpenAI as a randomness seed.

### Model request options

Pass OpenAI Responses request-body parameters to `responses.create()`, such as
`model`, `input`, `instructions`, `reasoning`, `max_output_tokens`, `text` and
`tools`. OpenAI validates which options the selected model and account support.
The requested model must also be included in the round's frozen pricing file.

| Option | Behavior |
| --- | --- |
| `request_id` | An integer, default `0`. Repeat the same request and ID to reuse its saved result. Change the ID for another model call with the same request. Use the same IDs when replaying the miner. |
| `session_id` | Optional subscription routing header, included in request matching. Subscription mode only; accepts 1-1024 visible ASCII characters. |
| `service_tier` | Defaults to `"default"` when omitted. Other values, including `"auto"`, `"fast"`, `"priority"`, `"ultrafast"` and `"scale"`, are passed through for OpenAI to validate. The witness rejects explicit `"flex"` requests. |
| `stream` | Defaults to `False`, returning an OpenAI `Response`. `True` returns typed events after the full response has been saved. |
| `input` | A prompt string or a list of input items, including text and images. |
| `tools` | Defaults to an empty list. Supplied tool definitions are passed through. |
| `background` | `True` is unsupported. Omit it or use `False`. |
| `store` | Passed through in API-key mode. Always set to `False` in subscription mode. |

The complete serialized request body must fit within 8 MiB. Other JSON-serializable
OpenAI request-body fields are passed through. SDK HTTP options such as `timeout`,
`extra_headers`, `extra_query` and `extra_body` are unsupported.

Subscription mode also supplies empty `instructions` when omitted and converts a
string `input` into one user message. Both modes request streaming from OpenAI;
`stream` controls how the saved response is returned to the caller. These request
settings are applied before request matching.

OpenAI controls tier availability for each model and account. See the official
[Fast mode](https://developers.openai.com/api/docs/guides/fast-mode) and
[Ultrafast mode](https://developers.openai.com/api/docs/guides/ultrafast-mode)
documentation. All accepted non-Flex tiers use the round's frozen model token
rates for budget accounting.

The [miner runtime](../miner-runtime/) provides the runner and validator execution
hosts. Install it alongside this client (`python -m pip install -e miner-runtime`
from the repository root). The runner automatically observes the supplied function:

```python
from miner_runtime.runner import run_generation
from witness_client import WitnessClient

with WitnessClient("witness.json", hotkey=hotkey, subscription="codex") as client:
    source = run_generation(generate, frozen_png, seed, client, renderer)
```

The function manages no recordings. The host keeps execution observations under
`state_directory/executions/<namespace>/<input identity>/execution.json`.
Keep those files and the replay engine outside miner access. This helper does not
implement process isolation or freeze code, environment, external content or
renderer behavior. See the complete [contract and field inventory](../witness-gateway/PODS.md#execution-and-replay-contract).

Use `openai_api_key=os.environ["OPENAI_API_KEY"]` instead of `subscription="codex"`
for API authentication. Subscription credentials are refreshed locally and stay
hidden by the credential proof. For the Pod's packaged renderer, construct
`RendererClient` with its pinned renderer identity and `transport=client.renderer_transport()`.

## Concurrency

`WitnessClient(..., parallel_preparations=3)` bounds simultaneous call
preparations across calls. The default is three and the range is 1 through
32. A slot covers dispatch, TLS, credential proving and durable authorization;
it becomes available while the provider runs. Native proving remains serialized.

The execution host can separately bound active captures using a shared `call_slots`
semaphore supplied to `WitnessClient`. The batch runner sets it from
`--parallel-calls`. Saved response reuse does not occupy a capture slot.

## Recovery and replay

Before the provider receives billable request bytes, the worker verifies the
credential proof and saves authorization. It then captures and persists the
response independently of the live client connection. Delivery waits and upload
retries reuse saved data.

The authenticated Pod supplies trusted completion metadata and expected evidence
hashes. Normal completion verifies the request and response bytes already held
by the client. Missing provider evidence bytes download directly from signed,
exchange-scoped R2 URLs and are checked against those hashes. Keep the bucket
private; an object fetched from an arbitrary CDN URL alone is insufficient.

Keep the original configuration, state directory, hotkey and authentication mode.
A `WitnessError.exchange_id` identifies the attempt. Retrieve it with
`client.resume(exchange_id)` during the access window. Repeated `run_generation()` restores completed observations and pending logical calls. A top-level aborted attempt retains its trace separately; another attempt recovers the saved calls.

`client.release(exchange_id)` releases a stuck capture without resetting usage.
A request already running may still finish and save evidence. Preserve its
recovery files and unresolved accounting. Releasing does not start another
provider request.

The validator combines host observations with native-verified captures, then supplies retained responses during replay. It matches per-key occurrences, causal delivery order, stream operations and output bytes. The [Pod guide](../witness-gateway/PODS.md) describes verified offline
replay, storage layout and lifecycle.

## Usage and evidence

The validator checks the frozen per-prompt allowance during replay, using distinct
captured calls consumed by the expected prompt. Reuse within one replay charges
once; each prompt consuming that call is charged independently. Other round work
need not be consumed. All input tokens use their full frozen weight, including
cached input; output includes reasoning once. Unknown usage remains unknown and
prevents budget acceptance. Proven non-dispatch failures have no paid charge.

Private provider evidence remains under `rounds/<round>/<hotkey>/<exchange>/`.
Execution observations belong to the host, separate from those captures.

See the [client reference](DEVELOPMENT.md#client-reference) for settings, recovery
and timeouts, and [TIMINGS.md](TIMINGS.md) for diagnostics. The client is
[MIT licensed](LICENSE); the wheel includes
[third-party licence notices](THIRD_PARTY_NOTICES.md).

[codex-cli]: https://learn.chatgpt.com/docs/codex/cli#install-codex

For subscription requests, an optional `session_id` sends a nonsecret routing
header across related calls. It is separate from `request_id` and
`prompt_cache_key`; it participates in request matching and is checked against TLS evidence.
`ExchangePending` reports an unresolved capture that should be resumed without
starting another inference.

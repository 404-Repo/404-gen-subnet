# Python WitnessClient

Send OpenAI requests through the subnet witness using Python. The witness
records the requests and responses so the validator can replay your submitted
code using those responses, without another OpenAI generation.

Use your registered Bittensor hotkey and your own OpenAI API key or ChatGPT
subscription. Install `404-witness-client` and import it as `witness_client`.

See the [sequence diagrams](SEQUENCE.md) for how requests pass through the
witness.

## Install

For the complete miner setup, use the [launch quickstart](../miner-reference/README.md#launch-quickstart).
It installs the separate async base miner, authenticates your wallet, and runs
model calls, previews and final submission through your assigned Pod.

The launch source tag is `witness-client-v0.2.14`. Its packages are
`404-witness-client==0.2.14`, `404-renderer-client==0.1.4`, and the optional
`404-miner-runtime==0.1.2`. They are installed from source; registry publication
is separate. Use Python 3.11+ on Linux x86_64 with glibc 2.34+, Go 1.27.3 and a C
compiler. The base miner requires Python 3.13. To install just the clients:

```sh
git clone --branch witness-client-v0.2.14 --depth 1 \
  https://github.com/404-Repo/404-subnet.git 404-sdk
cd 404-sdk
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install build==1.6.1 setuptools==84.0.0 wheel==0.48.0
(cd witness-client && GOTOOLCHAIN=local python build_wheel.py)
python -m pip install ./renderer-client ./witness-client/dist/*.whl
python -m pip check
```

Keep this checkout for its matching public proof parameters. No parameter download
or local state directory is required. Use an absolute parameter path in the
configuration below. An editable install is optional for development; rebuild the
native library and refresh metadata after changing source. See the
[development guide](DEVELOPMENT.md#build-and-verification) for the Linux release
wheels, verification and macOS source builds. Exact prepared artifact hashes and
verification scope are recorded in [release.json](release.json) on the public branch;
that manifest names the source commit used to build them.

Use the distribution name `404-witness-client`; `witness-client` is a different
package.

## Configure

Use the public gateway origin `https://witness.subnet404.xyz`. Save the configuration
as `witness.json`, replacing the paths:

```json
{
  "witness_url": "https://witness.subnet404.xyz",
  "proof_parameters": "/home/miner/404-sdk/witness-server/verifier/params"
}
```

The gateway selects its current competition round and returns its metadata.
The same configuration works across round changes and with an OpenAI API key or
a ChatGPT subscription. Submitted results and model recordings belong to the
assigned miner and round.

The gateway authenticates registration and returns the assigned Pod's public IP,
mapped TCP port, verified TLS name and scoped access grant. Calls, proofs,
call state and rendering use that Pod directly. An active assignment is
reused across transport reconnects and client restarts. Gateway control traffic
does not carry generation payloads.

The first operation waits for the assigned Pod to become ready. Subsequent
operations use that Pod directly; renewing its access grant keeps the same Pod.
If startup exceeds `delivery_timeout`, reconnecting discovers the current
allocation through the authenticated gateway. The SDK keeps no allocation,
job, proof, response, result or receipt files for recovery. A confirmed terminal
allocation permits a replacement.

Use absolute paths for proof parameters. The client checks those files before
loading them. Use a registered Finney netuid 17 hotkey. Load and unlock your wallet in the trusted runner and pass its
sr25519 hotkey to the client. Configuration, wallet material, subscription
credentials and proof parameters remain local inputs.

Native proving uses up to four Go CPUs by default, respecting a smaller runtime
CPU quota, with a 768 MiB Go soft memory limit. Set `GOMAXPROCS` and `GOMEMLIMIT`
before starting Python to choose different limits. The Go memory limit excludes
Python and other non-Go allocations. See the
[local synthetic benchmark](benchmarks/README.md) for measurement and tuning.

## Generate

An async miner exports `generate(image, seed, witness)` and awaits its model calls
and previews through the supplied witness. The execution host also supports
synchronous `generate(image, seed, witness, renderer)` programs. Model calls work
without prompt binding or recording lifecycle APIs. With the async client:

```python
response = await witness.responses.create(model=model, input=prompt, instructions="Return JavaScript.")
source = response.output_text
```

With `WitnessClient`, use the same call without `await`.

The default result is an OpenAI `Response`, including output items and typed usage.
Failed/incomplete Responses retain their status. Provider HTTP failures use SDK
status exceptions. `stream=True` returns typed SSE events with close/context-manager
support, buffered until the capture is sealed; it does not expose token timing.
Background generation and raw SDK transport options are unsupported. Connection
failures use `openai.APIConnectionError`; an expired wait uses
`openai.APITimeoutError`. Their cause retains the `WitnessError` and exchange ID.
An ambiguously dispatched paid request is never automatically repeated.

A direct `WitnessClient.responses.create()` also works for other work during the
round's access window. Equal provider/body/`request_id`/subscription-`session_id`
keys reuse a saved outcome throughout the miner's allocation scope. Use a stable,
different `request_id` for intentionally fresh inference. It is distinct from the
generation seed and is not sent to OpenAI as a randomness seed.

### Async calls

Use `AsyncWitnessClient` in an asyncio application. Its configuration and request
arguments match `WitnessClient`; HTTP and WebSocket traffic use native async I/O.
Proof initialization and credential proving run in worker threads. Local
subscription credential loading/refresh also runs off the event loop.

```python
from witness_client import AsyncWitnessClient, RenderOptions

async def generate_preview(hotkey, model, prompt):
    async with AsyncWitnessClient(
        "witness.json", hotkey=hotkey, subscription="codex",
    ) as client:
        response = await client.responses.create(model=model, input=prompt, request_id=0)
        return await client.render(response.output_text, RenderOptions(lighting="neutral"))
```

`await client.responses.create(..., stream=True)` returns an async iterator over
sealed, buffered events. Use `async with stream` or `await stream.aclose()` to
close it. Client resources close on `async with` exit or `await client.aclose()`.
Keep a client on one event loop and cancel/await its outstanding calls before
closing it.

Cancellation propagates as `asyncio.CancelledError` and closes the active network
stream or tunnel. An in-flight native proof may finish in its worker thread;
it cannot authorize a cancelled call. The Pod retains its call and recording.
Repeat the same request and `request_id`, or use `await client.resume(exchange_id)`,
to recover through the existing delivery policy. A fresh client needs the same
hotkey, provider and canonical request identity; it needs no generated local state.

Trusted async hosts can await `client.capture_context()`,
`client.capture_response(key, body)` and `client.saved_response(exchange_id)`.
Both client concurrency limits apply to async calls; excess calls wait inside
the SDK.

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

The SDK recovers model responses from authenticated Pod recordings. The optional
[miner runtime](../miner-runtime/) supplies execution hosts for validator replay;
its `run_generation()` helper writes execution observations under
`state_directory/executions/<namespace>/<input identity>/execution.json`. Those
host observations are separate from SDK recovery. This helper does not implement
process isolation or freeze code, environment, external content or renderer
behavior. See the [execution contract](../witness-gateway/PODS.md#execution-and-replay-contract).

Use `openai_api_key=os.environ["OPENAI_API_KEY"]` instead of `subscription="codex"`
for API authentication. Subscription credentials are refreshed locally and stay
hidden by the credential proof.

### Render previews

Use `witness.render(source, options)` or `await witness.render(source, options)`
with the async client. Both reuse the owned authenticated Pod connection and
return PNG bytes. No renderer identity, URL, transport or additional client is
required. `RenderOptions`, `RenderRejected` and `RenderUnavailable` are available
from `witness_client` and `renderer_client`.

Options match the renderer API; omitted options use studio lighting. Pass
`RenderOptions(lighting="neutral")` for neutral previews. Requests use a
240-second HTTP timeout, with no automatic retry or SDK render queue. JavaScript
is bounded to 1 MiB of UTF-8, PNGs to 32 MiB and rejection details to 256 KiB. Structured
422 responses raise `RenderRejected`; transport, service and invalid-response
failures raise `RenderUnavailable`. Preview renders do not submit results.
During execution and replay, the host routes `witness.render()` to its supplied
renderer. Render recording and persistence remain the host's responsibility.

The client uses `/v1` for gateway allocation, call snapshots and capture
WebSockets. Set `witness_url` to the gateway's HTTPS origin without a path.

## Submit the final result

After generation returns, submit its JavaScript or `None` through the connected
witness:

```python
result = witness.submit(image, source)
# With AsyncWitnessClient:
result = await witness.submit(image, source)
```

`image` contains the frozen prompt bytes. The SDK computes the image digest and
reuses its authenticated assigned Pod connection. The Pod uses its configured
round seed and admitted prompt set, then saves the final source, judging views
and embeddings. No renderer identity, URL, prompt digest or seed is supplied to
`submit()`. Images are bounded to 8 MiB and JavaScript to 1 MiB of UTF-8. The
returned dictionary contains `js`, `views`, `size` and `failure_reason`.

Submission belongs outside `generate()` and validator replay. Generation/replay
witness stubs reject submission and submitted-result lookup. The live runner reads submitted prompt identities before generation and
skips them by membership, including a prompt whose submitted source was `None`.

If generation or submission fails, report the failure and leave the prompt pending
for a later caller-controlled pass. Rerunning the algorithm with identical
canonical model requests and stable request IDs reuses the Pod's retained model
responses. Preview rendering may repeat; preview bytes embedded in a model request
must reproduce the same request body to reuse its recording. Changed requests can
start new inference. No local final-source or receipt file is required.

Each `submit()` makes one request. The SDK does not queue submissions or limit
submission concurrency. Pod overload responses such as `save_queue_full/503`
propagate to the caller, which can report the failure and try another pass later.

Submission has a 150-second request timeout. Errors raise `WitnessError`, retaining
the Pod's `status_code` and `code` when available and the HTTPX cause for transport
failures. There is no automatic submission retry. Async cancellation propagates
and the Pod may finish saving independently. An identical submitted result returns
the existing result; different source, including a change between JavaScript and
`None`, raises a conflict.

### Read submitted results

The live runner can resume a batch by reading saved results from the assigned Pod:

```python
from pathlib import Path

submitted = witness.get_submitted()
# With AsyncWitnessClient:
submitted = await witness.get_submitted()

for image_url in prompt_urls:
    if Path(image_url).stem in submitted:
        continue
    # Download this image, generate, then submit its returned source or None.
```

The dictionary is keyed by the official prompt ID, the filename stem in the
round's frozen prompt URLs. The Pod resolves image SHA256 hashes through its
frozen round metadata; filenames need not match image hashes. Each value
contains the same `js`, `views`, `size` and `failure_reason` fields as `submit()`.
It includes saved failures such as a submitted `None` or a renderer rejection;
those are completed outcomes. Source and view fields identify stored artifacts.
An empty dictionary means no results have
been saved for this miner in the assigned Pod's round.

Lookup reuses the authenticated Pod connection and reads the durable result index.
It does not run inference, render, submit, or require the local output checkpoint.
After a lost submission reply, read this index before rerunning generation for
anything still pending. A result appears only after it has been committed; an
absent entry may still be saving. Server and transport failures raise
`WitnessError`, with no automatic retry. Generation and replay stubs reject lookup;
it belongs to the live runner.

The existing `ArtifactClient.save_final()` API remains supported. Its explicit
seed must match the configured round. `submit()` without a seed and
`get_submitted()` require a Pod supporting these APIs. The reference
`run_and_save()` helper still uses an explicit local checkpoint; it is separate
from the SDK's Pod-based recovery flow.

## Concurrency

Configure both clients with numeric limits:

```python
client = AsyncWitnessClient(
    "witness.json", hotkey=hotkey, subscription="codex",
    parallel_calls=4, parallel_preparations=2,
)
```

`WitnessClient` accepts the same limits. Both default to three and accept integers
from 1 through 32. `parallel_calls` bounds active captures across all prompt
pipelines sharing the client. `parallel_preparations` bounds TLS setup, credential
proving and authorization within those captures. Preparation capacity releases
after authorization, so provider inference can overlap with later preparations.
Native proving remains serialized.

Concurrent async `responses.create()` calls queue inside the SDK when capacity is
full. Synchronous calls obey the same limits when invoked concurrently. Callers
do not need executors, semaphores or locks to enforce either limit. Completed
response reuse bypasses both capacities; `resume()` retrieves an existing exchange
without starting inference. Failure and cancellation release acquired capacity,
including when a call was waiting for preparation. Cancelling a queued call does
not cancel the calls ahead of it. Cancelling delivery does not stop the Pod's
independent capture; retain the call identity for recovery.

The synchronous `call_slots` argument remains accepted for existing hosts as an
additional limit. It is optional; the SDK always owns and applies its numeric
limits. Separate client instances have separate limits.

## Recovery and replay

Before the provider receives billable request bytes, the worker verifies the
credential proof and saves authorization. It then captures and persists the
response independently of the live client connection. Delivery waits and upload
retries reuse saved data.

`responses.create()` starts or joins the same logical call, polls unfinished
captures and retrieves the saved outcome internally. Its capture/recovery wait
uses `delivery_timeout` and the server's capture deadline; safe replacement
attempts share that wait window. Each REST request has a thirty-second timeout.

The authenticated Pod supplies trusted completion metadata and expected evidence
hashes. Normal completion verifies the request and response bytes already held
by the client. Missing provider evidence bytes download directly from signed,
exchange-scoped R2 URLs and are checked against those hashes. Keep the bucket
private; an object fetched from an arbitrary CDN URL alone is insufficient.

Use the same gateway, hotkey, provider and canonical request identity.
Repeating `responses.create()` with the same request and `request_id` retrieves or
continues waiting for that call, including through a fresh client after losing
all generated local state. `resume(exchange_id)` retrieves and verifies a saved
attempt during the access window using the exchange ID from `WitnessError`.
The SDK downloads retained request bytes when needed to verify their identity.

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

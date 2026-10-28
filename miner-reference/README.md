# 404-GEN Miner Guide

Submit a Python repository with a `generate` function in its root `main.py`.
The sandbox calls it for each prompt image and receives a Three.js module.

Use the [async base miner](https://github.com/404-Repo/404-base-miner-gpt)
for the launch path below. It exports `async generate(image, seed, witness)`,
uses `AsyncWitnessClient` for model calls and Pod previews, and submits the
returned JavaScript through `await witness.submit(image, source)` in its runner.
The synchronous four-argument reference program in this directory is also
supported; its helper and replay documentation follows the quickstart.

## Launch quickstart

Use Linux x86_64 with glibc 2.34+, Python 3.13, Go 1.27.3, a C compiler and Git.
Use a local sr25519 wallet hotkey registered on **Finney netuid 17**. The public
witness origin is **https://witness.subnet404.xyz**. The gateway assigns your
round and Pod; generation, rendering and submission use that authenticated Pod.

Install these matching launch sources into sibling directories. The SDK packages
are `404-witness-client==0.2.13rc9` and `404-renderer-client==0.1.3rc1`.
`404-miner-runtime==0.1.1rc1` is optional for the synchronous execution/replay
helpers and is not needed by this base miner.

```sh
git clone --branch witness-client-v0.2.13rc9 --depth 1 \
  https://github.com/404-Repo/404-subnet.git 404-sdk
git clone https://github.com/404-Repo/404-base-miner-gpt.git 404-base-miner
git -C 404-base-miner checkout 26c3ad32f9c4f202c9cd896c955695ab1964b75c
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install build==1.6.1 setuptools==84.0.0 wheel==0.48.0
(cd 404-sdk/witness-client && GOTOOLCHAIN=local python build_wheel.py)
python -m pip install ./404-base-miner
python -m pip check
cd 404-base-miner
```

The base miner's package metadata resolves the clients from `../404-sdk/`.
These pip commands build and install those local packages without relying on a
previous Poetry lock. Keep the SDK checkout and its three proof-parameter files.
Create `witness.json` at the base miner's root with the absolute parameter path:

```sh
python - <<'PYCONFIG'
import json
from pathlib import Path
config = {
    "witness_url": "https://witness.subnet404.xyz",
    "proof_parameters": str(Path("../404-sdk/witness-server/verifier/params").resolve()),
}
Path("witness.json").write_text(json.dumps(config, indent=2) + "\n")
PYCONFIG
```

No `state_directory`, separate renderer or local result checkpoint is needed.
For API billing, set `OPENAI_API_KEY` in your environment and add `--api` below.
For subscription billing, install the [Codex CLI](https://learn.chatgpt.com/docs/codex/cli#install-codex)
and sign in with your ChatGPT subscription on the miner machine:

```sh
codex -c 'cli_auth_credentials_store="file"' login
```

The witness reads the local file login from `CODEX_HOME` or `~/.codex`; a keyring-only
login is insufficient. Set `codex_home` in `witness.json` for another login
directory, and keep that directory private. See the official
[authentication guide](https://learn.chatgpt.com/docs/auth). Load/unlock the named
wallet locally; its private key stays on your machine.

Download the current round's frozen `prompts.txt` and published seed from the
[active competition](https://github.com/404-Repo/404-active-competition).
`prompts.txt` contains one image URL per line. Use that round's integer seed;
keep official prompt filenames when downloading individual images.

```sh
python main.py --prompts /absolute/path/to/prompts.txt --seed PUBLISHED_SEED \
  --wallet-name miner --wallet-hotkey default
# For API billing, add --api to the same command.
```

The runner awaits `generate(image, seed, witness)` for each pending image.
Inside generation, await `witness.responses.create(...)` and
`witness.render(source, RenderOptions(lighting="neutral"))`. It then submits
`await witness.submit(image, source)` outside generation. The Pod stores the
final source, judging views and embeddings for collection; there is no second
upload command. `None` is a submitted failure outcome.

Each pass calls `await witness.get_submitted()` and skips saved prompt IDs,
including saved failures. It makes up to three passes by default. Rerun the
same command for remaining work; identical canonical model requests and stable
request IDs reuse retained Pod recordings. Keep source, images, seed, models,
hotkey and authentication mode fixed. Preview bytes must reproduce the same
model request body. No paid requests are needed for installation checks:
`python main.py --help` only loads the CLI.

## Synchronous reference program

Copy [main.py](main.py), [output_specifications.md](output_specifications.md) and
the [miner_reference package](miner_reference/) into your repository root.
For each image, the example generates one Astra and two Sol candidates concurrently,
renders their grids, and asks Sol to select a winner and improvements. Two concurrent
Sol calls independently refine that same winner. Sol then selects from the incumbent
and the valid refinements. The selected JavaScript is returned.
Each candidate is centered and scaled using the specified bounding-box calculation
before rendering, selection, or use as a refinement parent.

Production sandbox isolation and validator integration still need implementation.
Development helpers capture execution observations, save managed submissions and
run offline replay without process isolation.

## What to submit

Register a Finney netuid 17 hotkey and submit these chain commitment fields:

| Field | Value |
| --- | --- |
| `repo` | Repository URL containing `main.py` and its supporting source/data |
| `commit` | Full 40-character commit, fixed before prompts and seed are published |

Each submitting hotkey needs the 100-alpha lock. Multiple hotkeys per team are
allowed; the coldkey must cover all their locks. Admission and anti-spam limits
apply per hotkey, without guaranteed per-team queue fairness.

Commit every module, template, configuration file and data file that affects
generation. The runner snapshots this source and records its hash. A branch name
or moving tag cannot identify a submission. You supply Python source; the operator
provides the sandbox and clients.

**Multiple Python files and packages are allowed.** Keep `main.py` at the
repository root as the entry point, exposing `generate(...)`. It may import your
other committed modules and packages. All supporting files must be included in
the submitted commit and are available during both generation and replay.
The runner puts the submission root on Python's import path.

The example uses this layout:

```text
main.py
miner_reference/
    __init__.py
    pipeline.py
    images.py
output_specifications.md
```

[main.py](main.py) delegates to the model/render flow in
[pipeline.py](miner_reference/pipeline.py).
[images.py](miner_reference/images.py) converts PNG bytes into data URLs
for model requests. This is an example organization; keep `generate(...)` in the
root entry module and organize your supporting code as needed.

## The function

```python
def generate(image: bytes, seed: int, witness: WitnessClient, renderer: Renderer) -> str | None:
    ...
```

| Argument | Supplied by the runner |
| --- | --- |
| `image` | Frozen PNG prompt bytes, identical in generation and replay |
| `seed` | Published round seed, an integer from zero through 2^64 − 1 |
| `witness` | The `witness_client.WitnessClient` model-call interface |
| `renderer` | The `renderer_client.Renderer` rendering interface |

Use `seed` to initialize reproducible local randomness. The same value is supplied
during generation and replay; this example uses it to shuffle candidate order.

Return the final JavaScript string, or `None` to declare generation failure.
An uncaught exception is a miner error. The runner checks the return type, UTF-8
size and JavaScript rules.

By default, `witness.responses.create(...)` waits for the complete response and
returns an OpenAI `Response` object. Read text from `result.output_text` and token
usage from `result.usage`.
`renderer.render(source, options)` returns PNG bytes or raises a structured
`RenderRejected`. Send prompt images and rendered previews as PNG data URLs in
`input_image` items. You may render, compare and refine candidates within the
prompt's budget. A successful example run makes seven model calls and five
four-view grid renders. It uses medium reasoning, up to 32,768 output tokens per
candidate and 2,048 per selection. Candidate calls have distinct `request_id`
values, so the two identical initial Sol requests produce separate exchanges.
A rejected candidate gets one Sol repair attempt with its source and exact
validation errors, followed by normalization and another render. Repairs share
the model-call limit and have distinct `request_id` values. A prompt makes at
most twelve model calls and ten renders; valid candidates remain available when
a repair fails. Infrastructure failures propagate to the runner for recovery.
The runtime supplies a Pod witness or its offline replay counterpart. Model calls require no prompt binding or recording lifecycle API.

Both clients are supplied in generation and replay. Client construction,
credentials, recording and recovery are handled by the runtime. Your function
does not select replay mode, manage exchange IDs, parse a command line or handle
input/output directories.

The runner saves the exact returned JavaScript or explicit `None` in managed
submission storage. Prompt IDs are unique lowercase alphanumeric stems. Your
function handles one image per call; prompt count is a round setting.

## Run several prompts concurrently

The [batch runner](miner_reference/batch.py) runs independent prompt pipelines
against the Python CPU Pod witness. The execution host automatically observes each prompt execution. The three initial calls and two refinements run concurrently within
their stages; each selection waits for all its candidates. Model requests from
different prompts can overlap each other and rendering. Renders within a prompt
follow candidate order, independently of model completion order.

A batch may contain all 128 prompts or a larger manifest. `--parallel-prompts`
bounds active prompt pipelines and `--parallel-calls` bounds active inference
captures across them. Each pipeline can use up to three simultaneous model calls,
so eight active prompts can offer up to 24 calls, subject to the shared call limit.
Defaults remain four active prompts, two simultaneous model calls, and one render.
All concurrency settings accept 1 through 32. Match `--parallel-calls` to the witness
Pod's configured `WITNESS_MAX_CAPTURES`, and `--parallel-renders` to renderer capacity.
For example, eight prompt pipelines with a 12-call limit require a witness Pod
configured and sized for 12 concurrent captures. The Pod default is two.

Use the pinned `witness_client`, `renderer_client` and `miner_runtime` packages supplied
by your runtime. The runner in `miner_runtime.runner` owns generation and recovery of
execution observations; `miner_runtime.validator` owns offline replay and prompt
budget acceptance. From this checkout, install `miner-runtime` with
`python -m pip install -e miner-runtime` and put `miner-reference` on `PYTHONPATH`. The witness
configuration includes the public gateway URL and matching proof parameters; see
the [client configuration](../witness-client/README.md#configure). `state_directory`
is an optional hint for execution-host observations, not an SDK recovery requirement.
This reference driver keeps its own private `--output` checkpoints.
The renderer URL and identity refer to an existing renderer. Authentication uses
the named local wallet and Codex subscription, or `--api` with `OPENAI_API_KEY`.
Set `RENDERER_API_KEY` when the renderer requires authentication.

The manifest has a published seed and frozen local image references:

```json
{
  "seed": 42,
  "prompts": [
    {"id": "pumpkin", "path": "/private/prompts/pumpkin.png", "sha256": "IMAGE_SHA256"}
  ]
}
```

IDs are unique lowercase alphanumeric strings; SHA256 values have 64 hexadecimal
characters. Image bytes must be distinct within a batch. An optional `url` is
descriptive metadata. The experiment manifest containing `seed` and `prompts`
with these fields is also accepted; additional provenance fields are retained
by the experiment and ignored by this runner.

```sh
PYTHONPATH=miner-reference python -m miner_reference.batch check \
  --manifest /private/prompts/manifest.json \
  --witness-config /private/witness.json \
  --output /private/miner-batch \
  --renderer-url http://127.0.0.1:18040 \
  --renderer-id PINNED_RENDERER_ID \
  --wallet-name miner --wallet-hotkey default \
  --parallel-prompts 4 --parallel-calls 2 --parallel-renders 1
```

Use the approved worker image digest as `--renderer-id`. Previews use the Pod renderer
unless `--renderer-url` selects another preview renderer. Intermediate previews stay
local to the runner; the Pod and R2 retain only the final submission. Final saving sends the returned JavaScript
or explicit `None` to the Pod. The Pod saves the JavaScript, prepares and saves the
judging views, grid and embeddings, then reports success. Saving errors are returned
for retry; save capacity is controlled by the Pod. The runner checkpoints returned bytes before final-save delivery. Reuse checks the
prompt hash, seed, renderer identity and approved source commit read from the Pod.
Retry the same batch output directory after a lost acknowledgement or Pod replacement;
it resumes final saving without rerunning completed generation. Infrastructure failures
remain pending. The gateway must have matching [artifact configuration](../witness-gateway/README.md#artifact-storage).

`check` verifies the frozen inputs and settings and prints the planned call,
render and concurrency counts. It does not load credentials or contact services.
Use `run` in place of `check` to execute. A 128-prompt successful workflow has 896 model calls and
640 grid renders. Invalid or refused candidates are excluded; no valid initial
candidate or an invalid selection returns `None`. A rejected candidate may use the bounded repair calls described above.

Console progress shows prompt IDs, stage numbers, input/output tokens and session
elapsed time. The private output directory contains `run.json`, `progress.json`
and one subdirectory per prompt. Prompt directories retain model responses and
usage under `calls/`, source/PNG/rejection checkpoints under `renders/`, and the
final `selected.js` and `result.json`. The source, seed, inputs, witness
configuration, wallet identity and renderer identity must stay fixed on resume.
Concurrency settings may change between runs.
Recovery restores recorded invocation and delivery order before returning saved
responses; those waits do not occupy inference capture slots.

An infrastructure failure leaves that prompt `pending` while other prompts
continue. Repeat the same command to resume saved model responses and render
outcomes. Completed generation is reused and its final output is submitted again. Saved output and PNG hashes are checked
before reuse, and one operator holds the output directory lock. The witness owns
inference deduplication, shared prompt accounting and uncertain dispatches; a
failed connection never permits this runner to buy a replacement inference.
`run` exits with status 1 when any prompt is pending; declared failures are terminal.

This is a local batch driver. Production sandbox isolation, verified renderer
replay and receipt submission belong to the runtime described below.

Outside `generate`, the runner checkpoints the returned JavaScript or explicit
`None` before saving its final result through the witness Pod. It retries saving
those bytes after a lost acknowledgement or Pod replacement. The collector reads
the saved result record directly from the existing public R2 bucket. Replay validation compares
the reproduced return with this independent submission; a storage receipt does
not establish source reproducibility.

## Generated JavaScript

Each successful file exports exactly one synchronous default function:

```js
export default function generate(THREE) {
  const root = new THREE.Group();
  // Build the asset procedurally.
  return root;
}
```

Return a `Group`, `Mesh`, `LineSegments` or `Points`, centered in `[-0.5, 0.5]` on
every axis, Y-up, +Z forward. Use `three@0.183.2` and only allowlisted APIs.
`THREE` is available through the function parameter, not at module top level.
No imports, external assets, network calls or nondeterministic JavaScript.

| Limit | Maximum |
| --- | ---: |
| Source file | 1 MiB |
| String and numeric literals | 50 KB |
| Module evaluation plus function execution | 5 seconds |
| JavaScript heap | 256 MB |
| Vertices | 250,000 |
| Draw calls | 200 |
| Scene depth | 32 |
| Instances | 50,000 |
| Texture data | 4 MiB |

The [JavaScript reference](output_specifications.md) contains the full API allowlist,
counting rules and failure codes. [AGENTS.md](AGENTS.md) is a compact reference for
code-generation models.

### Rendering

The runtime fixes the renderer build and default options. The supplied
`renderer.render(source)` produces a four-view preview grid; use `RenderOptions`
for explicit preview settings. The runtime records those options for replay.
Final judging uses `render-service-js` 0.4.8 with eight white views, four gray views
and a grid. The local browser viewer is for inspecting assets, not reproducing
judging conditions.

## Python environment

The runtime uses Python 3.13 on Linux x86_64.
[runtime/requirements.txt](runtime/requirements.txt) lists the direct dependencies
and pinned versions. Generation, replay and incumbent runs use the same Python
build, installed packages, bundled clients and renderer configuration.

Miner code can use the standard library, OpenAI request/result types, Pydantic,
NumPy and Pillow. NumPy supports image arrays, masks, geometry calculations and
seeded parameters; Pillow supports decoding, cropping, resizing and encoding images.
Packages and subprocesses obey the same sandbox I/O restrictions as Python code.
Do not install or upgrade packages during generation or replay.

Miners can propose additional libraries in a pull request. Include a visual
explanation showing how the library would improve generation, such as before/after
renders, annotated images or geometry previews, and a small reproducible example.

## Reproducibility

Use the supplied seed for local randomness. For NumPy, choose an explicit
generator such as `Generator(PCG64(seed))`. Use stable ordering, explicit rounding,
and fixed image modes, resize filters and encoding settings. Test the resulting
request and output bytes on the supported runtime profile.

Wall clocks, random UUIDs, Python's randomized `hash()`, directory order and worker
scheduling must not decide requests or output. Seeded Python generation is allowed;
randomness inside the generated JavaScript is forbidden.

The runner fixes `PYTHONHASHSEED=0`, `OPENBLAS_NUM_THREADS=1`,
`OMP_NUM_THREADS=1` and `MKL_NUM_THREADS=1` before Python starts. A seed and a single
thread alone do not guarantee identical numerical or encoded results.

Use committed source/data, the frozen prompt and environment, recorded model
responses and verified renders. Local deterministic image processing is allowed.
Additional remote inference, web fetches, host state, undeclared files, imported
meshes, pretrained-weight downloads and baked image textures cannot supply assets.

## Budget

Each prompt has a nominal **5,000,000-credit budget and a 60-call cap**, shared across
all models and the generation, refinement, criticism and comparison calls consumed by its replay.
One credit represents $0.000001 of Standard API usage, so the allowance is equivalent
to $5. Rejected and discarded candidates count when their calls are consumed. Exactly 5,000,000 credits and
exactly 60 calls are within the nominal limits. Restarts, credentials and model
switches do not reset them.

**Unused budget cannot transfer between prompts or offset another prompt's overrun.**
Overrun allowances, any additional submission-wide limit and penalties remain
undecided.

The rate file gives credits per token. Add every call's charge for the same prompt:

```text
call_credits = input_tokens * input_weight + output_tokens * output_weight
prompt_credits = sum(call_credits)
```

Sum fractional credits exactly, without rounding each call.

Use verified provider `usage.input_tokens` and `usage.output_tokens`, including
image inputs and conversation history. Count all input tokens **once at the full
published input rate**, including cache reads and writes. Cached/write token
details are already included: do not subtract them, add them again or apply a
cache-write premium. Output already includes reasoning tokens; do not add them
again. `total_tokens` is only a consistency check.

API keys and ChatGPT subscriptions use the same competition weights. Standard,
Fast and Ultrafast are allowed and counted at Standard rates; Flex is forbidden.
Actual bills, discounts, subscription limits and faster-tier premiums do not
change competition accounting. The runtime and validator must enforce this on
both API and subscription routes, explicitly defaulting to Standard so account
defaults cannot select Flex.

The [rate file](budgets/pricing.json) contains all seven candidate
models, checked 2026-10-19. Values below are **credits per token**. With this credit
scale, the numbers also equal the published USD price per million tokens: 2 credits
per token represents $2 per million tokens.

| Model | Input | Output | Long-context input | Long-context output |
| --- | ---: | ---: | ---: | ---: |
| `gpt-6-astra` | 10 | 50 | 20 | 75 |
| `gpt-6.1-sol` | 2 | 10 | 4 | 15 |
| `gpt-6-sol` | 2 | 10 | 4 | 15 |
| `gpt-6-luna` | 0.10 | 0.50 | 0.20 | 0.75 |
| `gpt-5.6-sol` | 4 | 20 | 8 | 30 |
| `gpt-5.6-terra` | 2 | 12 | 4 | 18 |
| `gpt-5.6-luna` | 0.20 | 1.20 | 0.40 | 1.80 |

`long_context_above_input_tokens` is a pricing threshold for **one model request**,
checked against `usage.input_tokens`, including images and conversation history.
More than 272,000 input tokens switches the **whole request**, including output,
to long-context weights. Exactly 272,000 uses the short tier. It is a pricing
threshold, not an input limit or a total across the prompt's calls. Weights use the
official [Standard prices](https://developers.openai.com/api/docs/pricing);
each model's source is linked in the rate file. The credit scale and weights are
frozen for the round, even if provider prices change.

For example, Sol 6.1 with 10,000 input and 20,000 output tokens uses
**220,000 credits and one call**, equivalent to $0.22 at Standard prices.

Each round freezes the rate-file bytes/hash, exact allowed model IDs, endpoint
mappings and context tiers. The witness supports API and Codex subscription
Responses routes; access and usage support must be confirmed for each allowed
combination. UI labels and undocumented aliases do not establish a pricing
identity. Unknown model IDs cannot receive a guessed rate. Other providers or
billable features need explicit evidence and accounting before being allowed.

### Calls and failures

Every distinct request dispatched to OpenAI counts once, including HTTP 429 and
other provider rejections. Sending another request after an error counts again if
dispatched. Recovering or downloading the same exchange does not add a call or
charge its tokens again.

Proven pre-dispatch failures, worker allocation, proof setup, polling, rendering,
uploads and evidence reads are not model calls. An allocated exchange ID alone
does not prove dispatch. Confirmed dispatches count even when final token usage
is unknown; unknown dispatch state stays unresolved.

Charge all verified final input/output usage, even for failed or discarded work.
Unknown usage stays pending, with a known subtotal and no claimed remaining
balance. A timeout does not prove zero usage. Settlement rules for proven unbilled
rejections and unresolved exchanges are still open. A known limit breach remains
a breach while another exchange is unresolved.

The runtime assembles accounting automatically; miners do not build a ledger.
Budget checking may happen during validation, without a realtime budget service.
Use conservative output limits where supported to reduce the risk of overshooting.

## Recording, replay and retries

The runtime runs the same `main.py` in a fresh sandbox for generation and replay.
Source and environment are read-only; scratch space is discarded between attempts.
Recording and isolation start before importing `main.py` and cover libraries,
native code and child processes. All permitted external traffic goes through the
supplied clients. Direct network and alternate host-service access are blocked.
Stdout/stderr are diagnostics and never become generation inputs.

During generation, clients return live model responses and rendered images.
During replay, model results come from verified recordings. Every render,
including discarded candidates and structured rejections, is independently
reproduced and verified before its original recorded result is returned. Replay
has no live inference access. Calls, their order and final JavaScript bytes or
`None` must match the recorded run.

The execution host keeps witness observations outside miner scratch space. Logical
model calls are identified by provider, canonical request body, `request_id` and
optional subscription routing header inside the authenticated miner/round scope.
Identical keys reuse a captured outcome across prompts; generation seed is not a
model-call identifier or an OpenAI randomness seed. Causal entry/delivery order
and per-key occurrences reproduce visible results, failures and stream use without
using a global ordinal as the logical request identity.

The validator attributes budget by executing the expected prompt. Reuse of one
captured call inside that replay charges once. A different prompt consuming the
same call is independently charged the full verified cost. Unrelated captured work
need not be consumed. Known paid failures count; missing usage remains unknown and
cannot pass budget acceptance. Proven non-dispatch has no paid charge. The live
witness permits other work during the round's access window and does not enforce
prompt labels or a prompt-hash allowlist.

Replay compares normalized requests, stream operations, causal observations and
final output against verified evidence. It never issues inference or fabricates
missing work. Renders still require exact source/options/build behavior and their
own reproducible outcomes. The witness helpers do not implement the sandbox,
renderer replay or control arbitrary Python scheduling. These remain requirements
of the complete runtime described here, not evidence of its deployment.

Recovery joins ambiguously dispatched calls and permits replacement only after
proven non-dispatch. A process restart restores saved observations; a top-level
aborted attempt retains its trace separately and can retry using saved calls.
Completed executions keep their original visible errors even if a later delivery
would succeed. Provider HTTP failures use SDK exceptions; failed/incomplete
Responses remain typed objects. Infrastructure failures remain separate and can
leave usage unknown. See the [complete capture/replay contract](../witness-gateway/PODS.md#execution-and-replay-contract).

The default retry policy is **three attempts per prompt per phase** (generation
and validation), with delays of **one second, then two seconds**. Verification,
sandbox startup and execution share the attempt limit; failed startup counts.
The runner retains attempt counters and the original deadline outside the sandbox
for each round/hotkey/prompt/phase. Polling, evidence checks and same-exchange
recovery stay within these limits, without nested retry allowances.

| Outcome | Runtime action |
| --- | --- |
| Temporary sandbox, worker, renderer or evidence-storage failure | Stop the old process tree; retry within the attempt limit and original deadline |
| Possible model dispatch without a delivered result | Recover that same exchange |
| Miner error, confirmed resource violation, reproducible rejection, changed replay call/output or invalid evidence | Preserve/report the outcome; do not retry to obtain a different result |
| Infrastructure still unavailable after the retry limit or deadline | Leave the result unresolved |

Both phases share the same prompt's usage ledger. Retries, runner/worker restarts,
model changes and credential changes reset neither budgets nor attempt/deadline
limits. Retain known usage and dispatch counts even when no result is returned.
A known budget breach remains a breach while other work is unresolved.

Exact source hashes and decoded render pixels are compared. The round must
publish any render tolerance; exceeding it rejects the whole submission.
Tolerances cannot weaken authentication or allow invented/reused evidence.
Missing infrastructure evidence stays unresolved.

## Round settings and open decisions

The announcement specifies prompt count, capacity, generation window, first-seed
time, cutoff, runtime/source resource limits, allowed model/route combinations,
billable features, renderer options, retry policy and discrepancy tolerance.
The generation window is separate from each JavaScript module's five-second
execution limit.

These policies remain undecided:

- Additional images and shared adaptive work between prompts.
- Exact source-package restrictions and sandbox CPU, memory and process limits.
- Allowed model/endpoint features, including hosted tools or background requests.
- Overrun allowances, any additional submission-total limit and penalties.
- Render discrepancy thresholds and their per-prompt/per-submission scope.
- Partial submissions, receipt format, cutoff/settlement grace and final handling
  of proven unbilled rejections and unresolved usage.

Replay checks code, evidence, reproduced outputs and budgets; source audit and
quality judging are separate gates. Missing infrastructure results stay pending.
A round is finalized after its result is persisted.

The operator initializes its baseline with one generation attempt per prompt and
no best-of selection. Later incumbent runs use the admitted code and environment
on new prompts at the operator's expense, without resubmission by the incumbent.
Candidate audits use replay.

## Local validation

Start with [car.js](examples/car.js) for a hand-written module. The [examples](examples/)
also contain geometry/material patterns, prompt images and generated outputs.
Install the validator with Node.js 20 or newer, then validate from this directory:

```sh
npm --prefix validator ci
node tools/validate.js examples/car.js
```

Use `--json` for machine-readable results; see the [validator guide](validator/README.md).
To preview the examples, run `python3 -m http.server 8080` from this directory and
open the [browser viewer](http://localhost:8080/examples/viewer.html).

### Tests

Run the Three.js validator tests from this directory:

```sh
npm --prefix validator test
```

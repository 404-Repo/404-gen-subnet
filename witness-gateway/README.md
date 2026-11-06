# Witness gateway

The Python gateway authenticates Bittensor miners, assigns ordinary RunPod CPU
Pods and returns their direct TCP/TLS endpoints with scoped miner access.
Miners send calls, proofs and renderer requests directly to the assigned Pod.
Workers capture and verify OpenAI exchanges, then save evidence and usage in private R2 storage. A separate
revivor restores the gateway and drains resources owned by the deployment.

See [PODS.md](PODS.md) for configuration, lifecycle, recovery, budgets, replay and
the immutable runtime manifests. Both images are built from
[Dockerfile.pods](../witness-server/worker/Dockerfile.pods): target `gateway`
serves gateways and the independent `revivor`; target `production` serves miner
witness Pods. Both use `python -m pod_gateway`, defaulting to `gateway` and
`worker` respectively. Set `WITNESS_GATEWAY_IMAGE` to the gateway digest and
`WITNESS_IMAGE` to the witness Pod digest. Both pins are required.

The [client](../witness-client/README.md) makes allocation-scoped model calls; the execution host observes each generate() call for replay.
[The worker](../witness-server/worker/README.md) saves authorization before
releasing provider requests and persists responses before delivery. R2 contains
the durable call state and provider captures; RunPod remains the source of truth
for Pod identity and lifecycle.

Completion metadata comes from the authenticated Pod. Saved provider evidence
downloads use signed R2 URLs with size and SHA256 checks. The gateway carries
allocation and lifecycle control messages only.

Gateway and direct Pod API routes use `/v1`, including their WebSockets.
See the [route table](PODS.md#http-api). Configure clients with the gateway origin;
the unauthenticated gateway health check is `GET /health`.

## Local checks

Install the client development tools and native proof library as described in
[client development](../witness-client/DEVELOPMENT.md), then install
`requirements-pods.txt` with `constraints-pods.txt`. From this directory:

```sh
python -m pip install -c constraints-pods.txt -r requirements-pods.txt
python -m mypy
PYTHONPATH=.:../witness-client/src:../miner-runtime/src:../witness-server/worker:../renderer-client/src:../miner-reference:../shared python -m pytest -q
```

The tests use local provider, RunPod and storage fixtures. They do not provision
Pods or send paid inference requests. Deployment acceptance uses the exact image,
client, proof parameters and configuration in the
[release checklist](../docs/witness-server-release-plan.md).

## Model table

The **model table** is one JSON object in the configured R2 bucket at
`pod-pricing/v1/<deployment>.json`. It contains supported model IDs, token weights,
the context threshold and prompt limits. The operator's `publish-pricing` command
is its only writer. Updates may add models; existing rows, limits and routing
configuration cannot change or be removed.

With the operator's `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and
`R2_BUCKET` configured, publish from this directory:

```sh
python -m pod_gateway publish-pricing --deployment witness-public-v3 \
  --pricing ../miner-reference/budgets/pricing.json
```

The command writes the shared table; it does not build images or restart Pods.
Publish the initial table before starting workers. To add a model, append its row
to the input JSON and run the same command. Workers load the table at startup and
refresh it when a request names a model missing from their cache. An addition is
available to the next call in an existing generation. Known model rows remain
valid, so pending usage, retries and old captures keep their existing prices.
The 60-call cap, 5,000,000-credit allowance, cache accounting and context tiers
remain unchanged.

Gateway readiness checks the selected Pod and proof key. Pricing file hashes in
retained journals are informational; they do not require old and new tables to
match. Replay bundles still protect their own embedded pricing bytes by hash.
Images contain the pricing reader rather than a baked model table. This runtime
support must be installed once before model additions can be published without
rebuilding images.

## Artifact storage

`R2_BUCKET` holds final results, frozen prompt images and operational state.
`R2_REPLAY_BUCKET` holds provider evidence, intermediate renders and replay
artifacts. `R2_REPLAY_CDN_URL` is that bucket's public HTTPS origin. Both replay
settings are required.

The production replay bucket is `404-replays`, publicly available through
`https://replays.subnet404.xyz`, with a 10-day expiration policy. Final results
remain in `404-gen` at `https://subnet404.xyz` with 60-day expiration. Expiration
uses object age; operational state, pricing and frozen prompt inputs are excluded
from artifact expiration rules.

The Pod saves final results in the configured bucket under
`<storage-prefix>rounds/<full-round-name>/<hotkey>/submitted/<prompt-id>`. The empty
storage prefix preserves the official `404-gen` paths. The JavaScript is
`<prefix>.js`; white and gray judging views, `grid.png` and `embeddings.npz` are
stored under `<prefix>/`.

A final save supplies JavaScript or an explicit `None`. For JavaScript, the Pod
writes the source, renders the 12 canonical judging views and grid, computes
embeddings, and saves the result. One final save runs at a time per Pod, with up
to 256 additional saves waiting in arrival order until a slot becomes available.
A full queue returns HTTP 503 `save_queue_full`. Requests authenticate on arrival;
Pod selection and fresh round permission are checked before queued work starts.
The legacy `ArtifactClient` retries full-queue responses within its configured
retry window, using the same output. `WitnessClient.submit()` makes one request.
Preview rendering uses the renderer's existing concurrency limit.

The Pod saves original preview PNGs and structured renderer rejections in the
replay bucket at `<storage-prefix>rounds/<full-round-name>/<hotkey>/renders/v1/`.
Each filename is a SHA256 of the request body hash, raw path and query, and
trusted worker image. Identical requests reuse the saved outcome without a prompt
association. A PNG or rejection is saved before the response is returned;
infrastructure failures remain retryable errors.

Each miner has an allowance of 10,000 new successful preview renders per round.
The Pod lists saved PNGs once at startup and keeps the count in memory. Cache hits,
rejections, failed renders and failed uploads do not consume the allowance. New
cache misses return HTTP 429 `render_limit_exhausted` when it is exhausted;
existing cached outcomes remain available. Cache hits read their saved bytes from
R2. Final judging renders and validation reads do not consume this allowance.

The Pod publishes `<storage-prefix>rounds/<full-round-name>/<hotkey>/submitted.json`, mapping
official prompt IDs to records containing `js`, `views`, `failure_reason` and `size`;
`js` and `views` are storage keys. A saved final output cannot be replaced;
retrying the same output returns its saved result without rendering again.
Conditional writes merge concurrent index updates without losing other prompts.
An explicit `None` records `generation_failed`. A renderer rejection records
`render_rejected` and returns HTTP 422. Storage, rendering and embedding failures
return an error so the runner can retry saving. An incomplete save has no
completed result record.

The runner checkpoints returned JavaScript or `None` locally before network
delivery. Retrying the same output directory resends that result without running
generation again.

Set `DINOV3_REVISION` to the approved embedding-model commit and supply `HF_TOKEN`
for the gated DINOv3 model. These settings are forwarded to witness Pods.
Set `WITNESS_RENDERER_PROFILE` to JSON in the gateway/revivor environment:

```json
{
  "image_digest": "sha256:REPLACE_WITH_WORKER_IMAGE_DIGEST",
  "validation_sha256": "REPLACE_WITH_RENDERER_PROFILE_SHA256",
  "bootstrap_sha256": null
}
```

Use the immutable `WITNESS_IMAGE` digest. The gateway resolves the seed and prompt
mapping from the competition snapshot. Only the packaged renderer is supported;
`bootstrap_sha256` must be null. Compute its profile hash from the exact build
context, from the repository root:

```sh
PYTHONPATH=witness-gateway python -c 'from pathlib import Path; from pod_gateway.renderer import renderer_profile_sha256; print(renderer_profile_sha256(Path("render-service-js")))'
```

The optional legacy `WITNESS_ARTIFACTS` JSON can supply the renderer profile and
`commits` metadata. Its seed and prompt mapping are replaced by the resolved
round inputs at allocation. A configured commit is optional checkpoint metadata;
it does not restrict authenticated, assigned miners from saving. The async and
sync SDKs accept `submit(image, source)` without a seed or source commit. Explicit
legacy seeds must still match the frozen seed. Retained hash-keyed indexes recover
under official IDs without changing their artifact pointers, including saved `None` outcomes.

Set `witness_save_submissions` to `false` in the round schedule to disable final
storage explicitly. Submission endpoints return `artifact_storage_disabled`;
provider recordings and accounting remain durable. A round with final saving
enabled requires a renderer profile and embedding revision. Saved results remain
readable after admission closes. Retain results and evidence through judging and
the operator's retention period.

The [collector](../submission-collector/README.md) reads one result index per
round and hotkey, expands the storage keys with its existing `CDN_URL`, and
writes the per-miner result index to Git.

## Competition source and admission

Configure `WITNESS_COMPETITION_REPO` with a GitHub repository URL or `owner/repo`
and `WITNESS_COMPETITION_BRANCH` with the operator's chosen branch. Both default
to `404-Repo/404-active-competition` and `main`. Every branch follows the same
rules. A read-only `WITNESS_GITHUB_TOKEN` is required and is never sent to workers.
Finney membership, allocation capabilities, selected-Pod fencing and pricing
remain required. `WITNESS_TEST_HOTKEY` is unsupported. `WITNESS_ACTIVE_ROUND` and `WITNESS_ROUND_END` are not used for
automatic competition admission.

The gateway resolves the branch once per refresh and reads `config.json`,
`state.json`, and the selected round's `schedule.json`, `seed.json` and
`prompts.txt` at that commit. Every prompt image must download and verify before
allocation is enabled. Official IDs are filename stems; image SHA256s come from
the downloaded bytes. Repository-relative prompt paths resolve at the selected
commit. Content-addressed copies in R2 freeze external images as well.

Pods receive `WITNESS_ROUND_INPUTS` and their renderer settings at creation.
The inputs contain source identity and revision, round, seed, prompt content
references, hash-to-ID mapping, block deadline or explicit absence, and storage
scope. `GET /v1/artifacts/context` exposes these inputs and signed prompt URLs to
the assigned miner. Inputs are durably pinned per repository, branch and round.
Changing seed, prompt content/IDs, deadline or storage scope for an existing round
closes admission; publish a new round to use different inputs.

The gateway refreshes state every `WITNESS_ROUND_REFRESH_SECONDS` (default 60)
and sends policies over authenticated control sockets. Policies expire after
`WITNESS_ROUND_POLICY_SECONDS` (default 120, maximum 120). New allocations,
generation, previews and final saves require a fresh policy for the exact frozen
inputs and current `miner_generation` round. Finite schedules also require a fresh
Finney block below `generation_deadline_block`; leases are capped at the estimated
block boundary. `next_stage_eta` is approximate and is never a generation deadline.

Transient refresh errors retain valid permission until its original expiry and
never extend it. A newly observed source revision invalidates cached permission,
even when the new publication cannot be read.

A pause, closed stage or expired policy stops new work without draining Pods.
Captures check permission immediately before provider release;
queued saves check when execution begins. Already admitted work finishes retaining
its evidence. Status, saved-result lookup and identical saved submissions remain
available. A ready new round applies to new allocations; existing Pods retain
their frozen inputs. Grant renewal for an in-flight call or saved-result lookup
keeps the existing allocation. Replacing a previous round's Pod waits for its drain report.
The revivor leaves round selection and draining to the gateway.

Previously assigned miners can replace a lost Pod in the same current round for
saved-result lookup and recording recovery after closure. That allocation still
denies new generation, previews and final saves. New miners cannot allocate into
a closed round. Source identity must remain fresh for recovery allocation.

For a persistent round on any selected branch, publish ordinary round data such as:

`state.json`:

```json
{"current_round": 0, "stage": "miner_generation", "next_stage_eta": null}
```

`rounds/0/schedule.json`:

```json
{
  "earliest_reveal_block": 1,
  "latest_reveal_block": 2,
  "generation_deadline_block": null,
  "witness_storage_prefix": "witness-sandbox/",
  "witness_save_submissions": true
}
```

`rounds/0/seed.json`:

```json
{"seed": 42}
```

`rounds/0/prompts.txt`:

```text
https://your-prompt-host.example/chair.png
https://your-prompt-host.example/lamp.png
```

`config.json` supplies the competition name and existing competition fields.
The explicit null deadline keeps the collector in `miner_generation` indefinitely;
omitting the deadline is invalid. An unbounded round still needs fresh policies.
Use a distinct deployment for independently operated gateways and an explicit
storage prefix for isolated branch results, calls, accounting and recordings.
The collector uses the same prefix. Offline export accepts `--storage-prefix`.
The empty prefix retains all official paths. Repeated runs of the same persistent
round resume its immutable saved results; use a new round for new submissions.

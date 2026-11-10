# Witness gateway

The Python gateway authenticates Bittensor miners, assigns ordinary RunPod CPU
Pods and returns their direct TCP/TLS endpoints with scoped miner access.
Miners send calls, proofs and renderer requests directly to the assigned Pod.
A separate revivor restores the gateway and drains resources owned by the deployment.

See [PODS.md](PODS.md) for configuration, lifecycle, recovery, budgets, replay and
the immutable runtime manifests. Both images are built from
[Dockerfile.pods](../witness-server/worker/Dockerfile.pods): target `gateway`
serves gateways and the independent `revivor`; target `production` serves miner
witness Pods. Their entrypoints are `python -m pod_gateway` and
`python -m witness_pod`, respectively. Set `WITNESS_GATEWAY_IMAGE` to the gateway digest and
`WITNESS_IMAGE` to the witness Pod digest. Both pins are required.

The [client](../witness-client/README.md) makes allocation-scoped model calls; the execution host observes each generate() call for replay.
[The worker](../witness-server/worker/README.md) saves authorization before
releasing provider requests and persists responses before delivery. R2 contains
the durable call state and provider captures; RunPod remains the source of truth
for Pod identity and lifecycle.

The gateway carries allocation and lifecycle control messages only.

Gateway and direct Pod API routes use `/v1`, including their WebSockets.
See the [route table](PODS.md#http-api). Configure clients with the gateway origin;
the unauthenticated gateway health check is `GET /health`.

## Local checks

Install the client development tools and native proof library as described in
[client development](../witness-client/DEVELOPMENT.md), then install
`requirements-pods.txt` with `constraints-pods.txt`. From this directory:

```sh
python -m pip install -c constraints-pods.txt -r requirements-pods.txt
python -m pip install --no-deps -e . -e ../witness-server/worker
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

See the [worker README](../witness-server/worker/README.md#rendering-and-artifact-storage)
for render caching, the per-round allowance and artifact storage.

Set `DINOV3_REVISION` to the approved embedding-model commit and supply `HF_TOKEN`
for the gated DINOv3 model. These settings are forwarded to witness Pods.
The gateway passes the frozen seed, official prompt IDs and saving flag through
`WITNESS_ROUND_INPUTS`. The immutable `WITNESS_IMAGE` digest identifies the Pod
and its packaged renderer.

Set `witness_save_submissions` to `false` in the round schedule to disable final
storage. Submission endpoints then return `artifact_storage_disabled`; provider
recordings and accounting remain available.

## Competition source and admission

Configure `WITNESS_COMPETITION_REPO` with a GitHub repository URL or `owner/repo`
and `WITNESS_COMPETITION_BRANCH` with the operator's chosen branch. Both default
to `404-Repo/404-active-competition` and `main`. Every branch follows the same
rules. A read-only `WITNESS_GITHUB_TOKEN` is required and is never sent to workers.
Finney membership, allocation capabilities, selected-Pod fencing and pricing
remain required in the default `WITNESS_MODE=competition`. `WITNESS_TEST_HOTKEY`
is unsupported in competition mode. `WITNESS_ACTIVE_ROUND` and `WITNESS_ROUND_END` are not used for
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
Changing seed, prompt content/IDs or storage scope for an existing round
closes admission; publish a new round to use different inputs. Deadline changes
apply to subsequently allocated Pods.

The gateway refreshes allocation admission every `WITNESS_ROUND_REFRESH_SECONDS`
(default 60). New allocations require a current `miner_generation` round;
`WITNESS_ROUND_POLICY_SECONDS` (default 120, maximum 120) bounds the age of the
gateway's source snapshot. Finite schedules require a fresh Finney block below
`generation_deadline_block`. The gateway converts that block deadline to a fixed
Unix timestamp using the observed block timestamp and twelve seconds per block,
and passes it as `WITNESS_ROUND_END` when creating a Pod. An empty value means
the schedule explicitly has no generation deadline. The fixed timestamp is saved
beside the frozen round inputs so a gateway restart preserves the allocation spec.
The collector's block
comparison remains authoritative. `next_stage_eta` is never used as a deadline.

Transient refresh errors retain valid permission until its original expiry and
never extend it. A newly observed source revision invalidates cached permission,
even when the new publication cannot be read.

A pause or closed stage stops new generation allocations. Existing Pods retain
their frozen inputs and fixed deadline; they do not consume gateway policies or
renew access. Pauses and deadline changes apply to newly allocated or replaced
Pods. Captures check the local deadline immediately before provider release;
queued saves check when execution begins. Already admitted work finishes and
retains its evidence. Status, saved-result lookup and identical saved submissions
remain available after the deadline. A ready new round applies to new allocations.
Replacing a previous round's Pod waits for its drain report. The revivor leaves
round selection and draining to the gateway.

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

## Separate persistent test deployment

Set `WITNESS_MODE=test` for a gateway dedicated to testing. This mode requires
`WITNESS_TEST_HOTKEY`, a deployment name beginning with `witness-test-`, and a
competition source branch other than `main`. Only the configured canonical hotkey
is admitted, without a Finney registration check. Requests still require normal
`btauth/1` signatures and nonce protection. Assigned-Pod ownership, allocation
capabilities, selected-Pod fencing and worker limits are unchanged.

Use a dedicated fixture branch with the persistent-round configuration above.
Its seed, prompts, stage and deadline are independent of the official competition.
Use separate private R2 storage and replay buckets, bucket-scoped credentials,
master key and upstream TLS certificates. Publish the model table under the test
deployment name. Keep a nonempty `witness-tests/` storage prefix. Advance the test
round for fresh final submissions; existing results cannot be overwritten.

The gateway/revivor environment selects the test worker with its own immutable
`WITNESS_IMAGE` digest. Changing that pin replaces only the test gateway; request
a fresh worker allocation to exercise it. Do not change production image pins or
restart production services. A test deployment can reuse the stable frontend
host with separate Caddy, cloudflared and revivor services. The supplied
`deploy/witness-test-*.service` units use independent configuration under
`/etc/witness-test`, state under `/var/lib/witness-test`, loopback port `18052`
and Caddy administration port `2020`. Point the test Tunnel at
`http://127.0.0.1:18052`; retain the production Tunnel configuration.

A small deployment uses `GATEWAY_VCPU=2`, `WITNESS_VCPU=8`,
`WITNESS_MAX_PODS=1` and `WITNESS_IDLE_SECONDS=1800`. An idle worker drains after
30 minutes without work and is deleted only after captures, renders and uploads
finish. Confirm the owned worker IDs are absent from RunPod when ending a session.
An active request or unfinished upload can extend the billed lifetime. The count
limit and accounting are not a dollar spending cap.

To suspend gateway compute between sessions, stop `witness-test-revivor.service`
and delete the exactly identified test gateway after checking that no test workers
remain. The public hostname and stored configuration remain. Retained R2 data,
registry images and any applicable traffic still incur charges. Start
`witness-test-revivor.service` to recreate the gateway; the next signed client
allocation creates a worker. Do not stop the shared frontend VM or production
services. Leaving the test revivor running recreates a stopped or deleted gateway.

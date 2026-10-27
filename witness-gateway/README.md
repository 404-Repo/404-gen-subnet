# Witness gateway

The Python gateway authenticates Bittensor miners, assigns ordinary RunPod CPU
Pods and returns their direct TCP/TLS endpoints with scoped miner access.
Miners send calls, proofs and renderer requests directly to the assigned Pod.
Workers capture and verify OpenAI exchanges, then save evidence and usage in private R2 storage. A separate
revivor restores the gateway and drains resources owned by the deployment.

See [PODS.md](PODS.md) for configuration, lifecycle, recovery, budgets, replay and
the immutable runtime manifests. The image is built from
[Dockerfile.pods](../witness-server/worker/Dockerfile.pods). Its entrypoint supports
`gateway`, `worker` and `revivor` roles through `python -m pod_gateway`.

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

## Artifact storage

The Pod saves final results in the existing public `404-gen` bucket under
`rounds/<full-round-name>/<hotkey>/submitted/<prompt-id>`. The JavaScript is
`<prefix>.js`; white and gray judging views, `grid.png` and `embeddings.npz` are
stored under `<prefix>/`.

A final save supplies JavaScript or an explicit `None`. For JavaScript, the Pod
overwrites the source, renders the 12 canonical judging views and grid, computes
embeddings, and saves the result. One final save runs at a time per Pod, with up
to eight additional saves waiting in arrival order for up to two minutes. A full
queue returns HTTP 503 `save_queue_full`; an expired queue wait returns HTTP 503
`save_queue_wait_timeout`. Requests authenticate on arrival; Pod selection and
the round cutoff are checked before queued work starts. The client retries these
two queue errors within its configured save timeout, using the same output.
Preview rendering uses the renderer's existing concurrency limit.

The Pod publishes `rounds/<full-round-name>/<hotkey>/submitted.json`, mapping
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
Set `WITNESS_ARTIFACTS` to JSON in the gateway/revivor environment:

```json
{
  "profile": {
    "image_digest": "sha256:REPLACE_WITH_WORKER_IMAGE_DIGEST",
    "validation_sha256": "REPLACE_WITH_RENDERER_PROFILE_SHA256",
    "bootstrap_sha256": null
  },
  "seed": 42,
  "prompt_ids": {"REPLACE_WITH_IMAGE_SHA256": "REPLACE_WITH_OFFICIAL_PROMPT_ID"},
  "commits": {}
}
```

Use the immutable `WITNESS_IMAGE` digest and frozen round seed. `prompt_ids` maps
each image's SHA256 to its official filename ID without the extension.
Supply the mapping from the same frozen round metadata; image hashes and
filename IDs are separate identities. Each image must resolve to one distinct ID.
The optional `commits` map supplies legacy checkpoint metadata; it does not
restrict miner admission or final saving. Legacy `prompt_hashes` configurations
retain their hash-based IDs. Only the packaged renderer is supported;
`bootstrap_sha256` must be null. Compute its profile hash from the exact build
context, from the repository root:

```sh
PYTHONPATH=witness-gateway python -c 'from pathlib import Path; from pod_gateway.renderer import renderer_profile_sha256; print(renderer_profile_sha256(Path("render-service-js")))'
```

Final saving is disabled when `WITNESS_ARTIFACTS` is absent. New saves require
the selected Pod and an open round. Saved results remain readable after cutoff
and Pod replacement. Retain result files through judging and the operator's
retention period.

The [collector](../submission-collector/README.md) reads one result index per
round and hotkey, expands the storage keys with its existing `CDN_URL`, and
writes the per-miner result index to Git.

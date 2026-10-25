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

## Local checks

Install the client development tools and native proof library as described in
[client development](../witness-client/DEVELOPMENT.md), then install
`requirements-pods.txt` with `constraints-pods.txt`. From this directory:

```sh
python -m pip install -c constraints-pods.txt -r requirements-pods.txt
python -m mypy
PYTHONPATH=.:../witness-client/src:../witness-server/worker:../renderer-client/src:../miner-reference python -m pytest -q
```

The tests use local provider, RunPod and storage fixtures. They do not provision
Pods or send paid inference requests. Deployment acceptance uses the exact image,
client, proof parameters and configuration in the
[release checklist](../docs/witness-server-release-plan.md).

## Artifact storage

Candidate JavaScript, previews and final choices are saved in the existing public
`404-gen` bucket under `artifacts/v1/<deployment>/<round>/<hotkey>/<prompt-sha256>/`.
Pods use `R2_BUCKET`; miners receive no storage credentials. The
[runner](../miner-reference/README.md#run-several-prompts-concurrently) checkpoints the returned JavaScript
or explicit `None` before saving the final choice and retries those same bytes
after a lost acknowledgement.

Set `WITNESS_ARTIFACTS` to JSON in the gateway/revivor environment:

```json
{
  "profile": {
    "image_digest": "sha256:REPLACE_WITH_WORKER_IMAGE_DIGEST",
    "validation_sha256": "REPLACE_WITH_RENDERER_PROFILE_SHA256",
    "bootstrap_sha256": null
  },
  "limits": {"max_renders": 16, "max_bytes": 268435456},
  "seed": 42,
  "prompt_hashes": ["REPLACE_WITH_FROZEN_PROMPT_SHA256"],
  "commits": {"REPLACE_WITH_ADMITTED_HOTKEY": "REPLACE_WITH_40_CHARACTER_SOURCE_COMMIT"}
}
```

Use the immutable `WITNESS_IMAGE` digest, frozen round seed and prompt hashes, and
admitted source commits. Only the packaged renderer is supported;
`bootstrap_sha256` must be null. Compute its profile hash from the exact build
context, from the repository root:

```sh
PYTHONPATH=witness-gateway python -c 'from pathlib import Path; from pod_gateway.renderer import renderer_profile_sha256; print(renderer_profile_sha256(Path("render-service-js")))'
```

The artifact API is disabled when `WITNESS_ARTIFACTS` is absent. Completed artifacts
survive Pod replacement and cutoff; new renders and final choices require the
selected Pod and an open round. Retain the artifact prefix through judging,
replay validation and the operator's evidence retention period.

See the [collector configuration](../submission-collector/README.md#artifact-configuration)
for collecting final choices and preparing judging images.

# Witness artifact storage

The artifact API saves candidate JavaScript and its preview PNG or structured
rejection in private R2, then saves an explicit final choice. The submission
collector reads that choice directly, prepares the existing judging bundle and
publishes its normal URLs. A storage receipt does not establish Python source
reproducibility or replace the validator and source-audit eligibility gates.

## Runner use

Construct one `ArtifactClient` per prompt outside submitted Python. The generator
keeps its `generate(image, seed, witness, renderer) -> str | None` signature.

```python
import hashlib
from pathlib import Path

from miner_reference.submissions import run_and_save
from renderer_client import ArtifactClient
from witness_client import run_generation

with ArtifactClient(
    "https://witness.internal",
    renderer_id=approved_worker_image_digest,
    round=round_name,
    prompt_sha256=hashlib.sha256(image).hexdigest(),
    seed=seed,
    submission_commit=admitted_source_commit,
    transport=witness.renderer_transport(),
) as renderer:
    receipt = run_and_save(
        lambda: run_generation(generate, image, seed, witness, renderer),
        renderer,
        Path(private_output) / "final.json",
    )
```

`render(source, options)` returns saved PNG bytes or raises `RenderRejected`.
`render_saved(source, options)` returns the receipt without downloading the PNG.
Neither selects the output. `save_final(output)` selects the exact source hash;
an edited or never-rendered string is saved through the renderer first. `None`
records a declared generation failure and cannot select an earlier candidate.

`run_and_save` writes the returned output to an atomic private checkpoint before
network delivery. A retry uses those bytes without calling the generator again.
The checkpoint binds the round, prompt, seed, renderer image and admitted source
commit. Its directory must remain private. Different final bytes require the
current receipt's `expected_revision`; stale replacements conflict. Model-call
capture and automatic execution observations remain separate from final artifact saving.
The validator compares replay output with this independently saved submission.

## Gateway and worker configuration

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

Use the actual immutable `WITNESS_IMAGE` digest. The renderer profile hash covers
the packaged renderer's JavaScript/HTML sources and dependency lockfile. Compute
it from the exact build context, from the repository root:

```sh
PYTHONPATH=witness-gateway python -c 'from pathlib import Path; from pod_gateway.renderer import renderer_profile_sha256; print(renderer_profile_sha256(Path("render-service-js")))'
```

Only the packaged renderer is supported; `bootstrap_sha256` must be null.
Startup rejects a different renderer profile or an image without the renderer.
The gateway validates the image binding, forwards the configuration to allocated
workers and restricts allocation to admitted hotkeys. Use the frozen round seed,
admitted source commits and `prompt_hashes` in this artifact configuration.
These input bindings govern artifact storage; model calls remain prompt-independent. Configuration changes conflict
with an existing prompt inventory. The API is disabled when this setting is absent.

Objects live under:

```text
artifacts/v1/<deployment>/<round>/<hotkey>/<prompt-sha256>/
    inventory.json
    sources/<sha256>.js
    images/<sha256>.png
    renders/<artifact-id>.json
    final.json
    judging/<source-sha256>/<judging-profile-and-options-hash>/
```

Keep the artifact bucket private and outside public CDN exposure. Miners receive
no storage credentials. Authenticated routes are `POST /artifacts/render`,
`POST /submissions` and `GET /submissions/<prompt-sha256>`.

## Submission collector

The collector accepts repo/commit reveals and freezes the admitted source in
submission metadata. Final results are collected from private Pod-managed artifacts.

After prompts and seed are selected, configure `WITNESS_ARTIFACTS_COLLECTION`
before DOWNLOADING:

```json
{
  "deployment": "REPLACE_WITH_DEPLOYMENT",
  "round": "REPLACE_WITH_FULL_ROUND_NAME",
  "bucket": "REPLACE_WITH_PRIVATE_WITNESS_BUCKET",
  "round_end": 2000000000,
  "prompt_hashes": ["REPLACE_WITH_FROZEN_PROMPT_SHA256"],
  "profile": {
    "image_digest": "sha256:REPLACE_WITH_WORKER_IMAGE_DIGEST",
    "validation_sha256": "REPLACE_WITH_RENDERER_PROFILE_SHA256",
    "bootstrap_sha256": null
  },
  "judging_profile": {
    "image_digest": "sha256:REPLACE_WITH_JUDGING_RENDERER_IMAGE_DIGEST",
    "validation_sha256": "REPLACE_WITH_JUDGING_RENDERER_PROFILE_SHA256",
    "bootstrap_sha256": null
  },
  "collection_delay_seconds": 300
}
```

The artifact bucket uses the collector's existing R2 account endpoint and operator
credentials. Its public uploads retain the existing output bucket and CDN.
`round_end` must equal the witness cutoff. The collector waits for cutoff plus the
settlement delay, verifies all frozen prompt bytes, and checks the selected source's
scope, round, seed, admitted commit, operation, manifest identity, size and SHA256.
The collector conditionally seals the final-choice key using its exact ETag. An
absent choice is sealed with a missing-output marker. A competing write either
wins before sealing and is collected, or fails its precondition afterwards. A
negative read alone never establishes a missing submission. Sealed choices retain
their receipt revision and permit idempotent reads, but cannot be replaced.

Pin `RENDER_URL` to the approved immutable judging build and describe that build in
`judging_profile`. These are operator deployment bindings; the collector does not
attest an external renderer. A saved grid is reused only when the complete profile
and all camera, angle, lighting, size, gap and background settings match. The eight
white and four gray views use the existing judging requests. Rejected preview
settings do not determine canonical judging validity.

The complete judging PNG bundle is saved privately before embeddings and public
upload. Retries reuse it, including after a lost manifest acknowledgement. Public
`views` appears only after all 12 views, grid and embeddings are uploaded.
`submitted.json` also records the source SHA256 and final-choice revision.

Missing final choices and explicit `None` are terminal delivery outcomes. Canonical
HTTP 422 is a terminal render rejection. Missing/changed stored bytes, incompatible
provenance, unavailable prompts, service failures, embedding failures and upload
failures stop collection for retry; they cannot advance the round to DUELS as
miner failures.

## Recovery and retention

Objects precede conditional, immutable render-manifest publication. Only published
results can be returned or selected. A pending render has a 300-second lease.
After expiry an unobserved result can be recomputed, with at most three total
attempts. The first complete manifest wins; recovery cannot replace an observed PNG.
Recovery-budget exhaustion returns `artifact_recovery_exhausted` (409). Active
leases return `artifact_pending` (503); storage quotas return 429.

Quotas reserve the maximum PNG/manifest bytes for all allowed attempts and retain
a reservation for possible orphaned uploads. Completed results release unused
capacity. Configure at least 102498304 bytes to admit one maximum-size request;
larger budgets allow concurrent candidate renders. R2 journal throttling retries
with the same write precondition. Uncertain conditional writes never authorize
new work. Completed records remain in R2 after cutoff and Pod replacement. An
available authenticated worker can return them; new renders and final choices
require the selected Pod and an open round.

Retain the private prefix until judging, replay validation and the evidence
retention period finish. Configure the bucket's lifecycle policy for the operator's
retention period; the service does not delete evidence. Recovered or orphaned bytes
remain covered by the round's storage budget until retention removes them.

## Offline checks

The gateway tests use authenticated localhost TLS, in-memory storage and synthetic
renderer PNGs. They do not provision Pods or make paid model calls.

```sh
cd witness-gateway
PYTHONPATH=.:../witness-client/src:../witness-server/worker:../renderer-client/src:../miner-reference python -m pytest tests/test_artifacts.py tests/test_settings.py -q
cd ../submission-collector
PYTHONPATH=../shared python -m pytest -q
```

Live R2 behavior, the built production image and browser rendering require the
existing deployment acceptance checks in [PODS.md](PODS.md).

# Submission Collector

Collects Python miner submissions from chain, collects their saved procedural Three.js modules, and prepares them for judging.

Miners submit a repository and an exact commit containing root `main.py` with
`generate(image: bytes, seed: int, witness: WitnessClient, renderer: Renderer) -> str | None`,
plus supporting committed source. The operator supplies a fixed runtime and library set.
See the current [miner guide](../miner-reference/README.md) for the contract.

The collector freezes the on-chain `repo` (`owner/repo`) and full 40-character
`commit` in `submissions.json`. Source-copy CI consumes those immutable entries
separately; the collector does not execute miner code or install dependencies.

The collector uses the existing public R2 bucket (`404-gen`) for artifacts and
judging outputs. Configure the approved renderer profiles and round bindings before
DOWNLOADING. The collector verifies the saved final choice and exact source
bytes, reuses compatible grids, and saves judging images for retries. Storage,
render-service and embedding outages keep collection pending. See
[artifact configuration](#artifact-configuration).

## Algorithm

1. **OPEN stage**: Wait for `latest_reveal_block`, then collect valid submissions within the inclusive reveal window. Keep registered subnet hotkeys whose coldkey covers the conviction lock (default 100 alpha per submitting hotkey, aggregated by coldkey; multiple hotkeys are allowed). Preserve reveal-block ordering, generate the seed and select the configured number of prompts. Publish the admitted list, seed, prompts and MINER_GENERATION state in one Git commit. If none are admitted, transition to FINALIZING.

2. **MINER_GENERATION stage**: Wait for `generation_deadline_block`. Miners use the published seed and prompts to generate JavaScript and the runtime saves each final choice through the witness Pod.

3. **DOWNLOADING stage**: For each submission, read its selected witness artifact, prepare PNG views and a grid, compute embeddings, and upload the bundle to R2. Save progress after each completed generation. Transition to DUELS when complete.

## State Files

Reads and writes to the competition Git repository:

| File | R/W | Description                            |
|------|-----|----------------------------------------|
| `state.json` | R/W | Current round and stage                |
| `config.json` | R | Competition configuration              |
| `prompts.txt` | R | Global prompt pool                     |
| `rounds/{n}/schedule.json` | R | Round timing (reveal blocks, deadline) |
| `rounds/{n}/seed.json` | W | Random seed for prompt selection       |
| `rounds/{n}/prompts.txt` | W | Selected prompts for the round         |
| `rounds/{n}/submissions.json` | W | Admitted repo, exact commit, reveal block and round |
| `rounds/{n}/{hotkey}/submitted.json` | W | JS URL, preview prefix, size and failure status |

## Artifact configuration

Set `WITNESS_ARTIFACTS_COLLECTION` to JSON after the round inputs are selected and
before DOWNLOADING:

```json
{
  "deployment": "REPLACE_WITH_DEPLOYMENT",
  "round": "REPLACE_WITH_FULL_ROUND_NAME",
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

`profile` must match the [Pod artifact configuration](../witness-gateway/README.md#artifact-storage).
`judging_profile` describes the approved immutable build served by `RENDER_URL`.
`round_end` must equal the witness cutoff; collection waits for the settlement
delay before sealing final choices. Artifact reads, sealing and judging-image
writes use the existing R2 client and credentials. Result URLs use the existing CDN.

## Development
```bash
poetry install
poetry poe lint
```

## Running
```bash
poetry run python -m submission_collector.main
```

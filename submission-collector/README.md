# Submission Collector

Collects Python miner submissions from chain, downloads their generated procedural Three.js modules, and prepares them for judging.

Miners submit a repository and an exact commit containing root `main.py` with
`generate(image: bytes, seed: int, witness: WitnessClient, renderer: Renderer) -> str | None`,
plus supporting committed source. The operator supplies a fixed runtime and library set.
See the current [miner guide](../miner-reference/README.md) for the contract.

The collector freezes the on-chain `repo` (`owner/repo`), full 40-character `commit`,
storage mode and optional `cdn_url` in `submissions.json`. Source-copy CI consumes those immutable
entries separately; the collector does not execute miner code or install dependencies.

With `SUBMISSION_SOURCE=witness`, a CDN is unnecessary. Set that mode before OPEN
admission, then configure the frozen private artifact bucket and renderer profiles
before DOWNLOADING. The collector verifies the saved final choice and exact source
bytes, reuses compatible grids, and saves judging images for retries. Storage,
render-service and embedding outages keep collection pending. See
[artifact configuration and recovery](../witness-gateway/ARTIFACTS_POC.md#submission-collector).

## Algorithm

1. **OPEN stage**: Wait for `latest_reveal_block`, then collect valid submissions within the inclusive reveal window. Keep registered subnet hotkeys whose coldkey covers the conviction lock (default 100 alpha per submitting hotkey, aggregated by coldkey; multiple hotkeys are allowed). Preserve reveal-block ordering, generate the seed and select the configured number of prompts. Publish the admitted list, seed, prompts and MINER_GENERATION state in one Git commit. If none are admitted, transition to FINALIZING.

2. **MINER_GENERATION stage**: Wait for `generation_deadline_block`. Miners use the published seed and prompts to generate `.js` files and save a final witness choice or upload to their CDN, according to the admitted storage mode.

3. **DOWNLOADING stage**: For each submission, read its selected private witness artifact or download its CDN `.js`, prepare PNG views and a grid, compute embeddings, and upload the bundle to R2. Save progress after each completed generation. Transition to DUELS when complete.

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
| `rounds/{n}/submissions.json` | W | Admitted repo, exact commit, CDN URL, reveal block and round |
| `rounds/{n}/{hotkey}/submitted.json` | W | JS URL, preview prefix, size and failure status |

## Development
```bash
poetry install
poetry poe lint
```

## Running
```bash
poetry run python -m submission_collector.main
```

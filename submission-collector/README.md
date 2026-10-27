# Submission Collector

Collects Python miner submissions from chain and records generated and submitted results.

Miners submit a repository and an exact commit containing root `main.py` with
`generate(image: bytes, seed: int, witness: WitnessClient, renderer: Renderer) -> str | None`,
plus supporting committed source. The operator supplies a fixed runtime and library set.
See the current [miner guide](../miner-reference/README.md) for the contract.

The collector freezes the on-chain `repo` (`owner/repo`) and full 40-character
`commit` in `submissions.json`. Source-copy CI consumes those immutable entries
separately; the collector does not execute miner code or install dependencies.

The Pod saves JavaScript, 12 judging views, a grid and embeddings in the existing
public R2 bucket (`404-gen`). It publishes one result index at
`rounds/<full-round-name>/<hotkey>/submitted.json`, mapping prompt IDs to records
with four fields: `js`, `views`, `failure_reason` and `size`. The prompt ID is the
stem of its published prompt URL.

The collector reads one complete index per miner over HTTP through `CDN_URL`,
expands the storage keys with that URL, and replaces `generated.json` for each miner
in Git. It needs no R2 credentials. A missing index (HTTP 404) or prompt entry becomes
`not_submitted`; an explicit `None` becomes `generation_failed`, and a renderer
rejection remains `render_rejected`. HTTP status errors, timeouts and connection
errors use two total attempts with a fixed three-second wait. The concurrency slot
is released between attempts. Exhausted retries leave collection retryable.

## Algorithm

1. **OPEN stage**: Wait for `latest_reveal_block`, then collect valid submissions within the inclusive reveal window. Keep registered subnet hotkeys whose coldkey covers the conviction lock (default 100 alpha per submitting hotkey, aggregated by coldkey; multiple hotkeys are allowed). Preserve reveal-block ordering, generate the seed and select the configured number of prompts. Publish the admitted list, seed, prompts and MINER_GENERATION state in one Git commit. If none are admitted, transition to FINALIZING.

2. **MINER_GENERATION stage**: Wait for `generation_deadline_block`. Miners use the published seed and prompts to generate JavaScript and the runtime saves each final result through the witness Pod.

3. **COLLECTING stage**: Collect generated and submitted results and record their public URLs and status. Save each miner's collected index. Transition to DUELS when complete.

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
| `rounds/{n}/{hotkey}/generated.json` | W | JS URL, preview prefix, size and failure status |

## Development
```bash
poetry install
poetry poe lint
```

## Running
```bash
poetry run python -m submission_collector.main
```

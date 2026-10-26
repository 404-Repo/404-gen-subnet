# Submission Collector

Collects Python miner submissions from chain, collects their saved procedural Three.js modules, and records their Pod-prepared judging bundles.

Miners submit a repository and an exact commit containing root `main.py` with
`generate(image: bytes, seed: int, witness: WitnessClient, renderer: Renderer) -> str | None`,
plus supporting committed source. The operator supplies a fixed runtime and library set.
See the current [miner guide](../miner-reference/README.md) for the contract.

The collector freezes the on-chain `repo` (`owner/repo`) and full 40-character
`commit` in `submissions.json`. Source-copy CI consumes those immutable entries
separately; the collector does not execute miner code or install dependencies.

The collector reads sources and completed judging bundles from the existing
public R2 bucket (`404-gen`). It derives the round name, seed, prompts and admitted
commits from the current competition state. The gateway publishes the deployed
round's cutoff, renderer identity, embedding revision and frozen input bindings at
`artifacts/v1/rounds/<full-round-name>.json` in the same bucket.

The Pod renders the 12 judging views and grid and computes embeddings when saving
the final output. The collector verifies the selected source and completed bundle,
seals the final choice, and records their existing public URLs in `submitted.json`.
A pending bundle or unavailable storage keeps collection pending.

## Algorithm

1. **OPEN stage**: Wait for `latest_reveal_block`, then collect valid submissions within the inclusive reveal window. Keep registered subnet hotkeys whose coldkey covers the conviction lock (default 100 alpha per submitting hotkey, aggregated by coldkey; multiple hotkeys are allowed). Preserve reveal-block ordering, generate the seed and select the configured number of prompts. Publish the admitted list, seed, prompts and MINER_GENERATION state in one Git commit. If none are admitted, transition to FINALIZING.

2. **MINER_GENERATION stage**: Wait for `generation_deadline_block`. Miners use the published seed and prompts to generate JavaScript and the runtime saves each final choice through the witness Pod.

3. **DOWNLOADING stage**: For each submission, verify its selected source and completed Pod judging bundle in R2, then record their public URLs. Save progress after each completed generation. Transition to DUELS when complete.

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

## Development
```bash
poetry install
poetry poe lint
```

## Running
```bash
poetry run python -m submission_collector.main
```

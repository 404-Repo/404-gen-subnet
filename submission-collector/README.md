# Submission Collector

Collects miner submissions from a chain, downloads generated 3D files, and prepares them for judging.

## Algorithm

1. **OPEN stage**: Wait for `latest_reveal_block`, then collect all valid submissions from a chain. Generate seed and select prompts for the round. Save to git and transition to MINER_GENERATION.

2. **MINER_GENERATION stage**: Wait for `generation_deadline_block`. Miners use the published seed and prompts to generate 3D files and upload to their CDNs.

3. **DOWNLOADING stage**: For each submission, download PLY files from miner CDNs, render PNG previews, upload both to R2. Save progress after each file for crash recovery. Transition to DUELS when complete.

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
| `rounds/{n}/submissions.json` | W | Miner submissions from chain           |
| `rounds/{n}/{hotkey}/submitted.json` | W | Downloaded GLB/PNG locations           |

## Development
```bash
poetry install
poetry poe lint
```

## Running
```bash
poetry run python -m submission_collector.main
```
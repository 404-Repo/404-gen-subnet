# Judge Service

Determines round winners by comparing miner outputs using a vision-language model.

## Algorithm

1. **Qualification**: Each miner competes against the base leader (previous round winner). Must win by ≥5% margin to qualify.

2. **Main timeline**: Qualified miners compete sequentially by submission time. Each winner becomes the new leader. All local leaders are sent for verification.

3. **Exploratory duels**: While waiting for verification, pre-compute all remaining pairwise matches. This prepares alternative timelines if verification fails.

4. **Verification check**: If all local leaders pass verification → round ends. If any fails → discard timeline, start new one with remaining miners.

5. **Repeat** until a fully verified timeline emerges.

## State Files

Reads and writes to the competition Git repository:

| File | R/W | Description |
|------|-----|-------------|
| `state.json` | R | Current round and stage |
| `rounds/{n}/matches_matrix.csv` | R/W | All match results |
| `rounds/{n}/require_audit.json` | R/W | Miners needing verification |
| `rounds/{n}/output_verification.json` | R | Verification results |
| `rounds/{n}/source_audit.json` | R | Source audit results |
| `rounds/{n}/{hotkey}/duels_*.json` | W | Detailed match reports |
| `rounds/{n}/winner.json` | W | Final winner |

## Development

```bash
poetry install
poetry poe lint
```

## Running

```bash
poetry run python -m judge_service.main
```
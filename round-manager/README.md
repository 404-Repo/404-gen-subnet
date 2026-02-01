# Round Manager

Manages competition round transitions, schedules new rounds, and updates leader state.

## Algorithm

1. **Wait for FINALIZING stage**: Only processes when the current round is in FINALIZING stage (after judge-service determines a winner).

2. **Compute next round start**: Calculate when the next round should begin based on:
   - Previous round start time
   - Configured round duration (days)
   - Finalization buffer (minimum time before next round)
   - Competition start/end dates

3. **Build next round schedule**: Create block ranges for the next round:
   - `earliest_reveal_block`: Previous round's latest + 1
   - `latest_reveal_block`: Block at next round start time

4. **Compute leader transition**: Determine leader changes:
   - If new winner: Create new leader entry with weight = 1.0
   - If leader defended: Decay weight (down to floor)
   - Update effective block for weight changes

5. **Commit atomically**: Write all updates in a single commit:
   - `state.json`: Next round number and stage (COLLECTING or FINISHED)
   - `rounds/{n}/schedule.json`: New round schedule
   - `leader.json`: Updated leader transitions (if changed)

## State Files

Reads and writes to the competition Git repository:

| File | R/W | Description |
|------|-----|-------------|
| `state.json` | R/W | Current round and stage |
| `config.json` | R | Competition configuration (dates, timing) |
| `leader.json` | R/W | Leader transition history |
| `rounds/{n}/schedule.json` | R/W | Round timing (reveal blocks) |
| `rounds/{n}/winner.json` | R | Round winner from judge-service |
| `rounds/{n}/builds.json` | R | Docker build info for winner |

## Development

```bash
poetry install
poetry poe lint
```

## Running

```bash
poetry run python -m round_manager.main
```
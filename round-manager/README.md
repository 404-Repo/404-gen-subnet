# Round Manager

Manages competition round transitions, schedules new rounds, and appends leader
history after judge-service publishes a winner. In the closed-source model
competition, miners submit a Python application with a committed root
`main.generate` entry point and supporting source. The operator supplies the
fixed runtime and installed packages. Round-manager records source identity
without building or executing miner code.

## Algorithm

1. **Wait for FINALIZING**: Only process after judge-service determines a winner.
2. **Compute next round start**: Use the previous round start, configured round
   duration, finalization buffer, and competition start/end dates.
3. **Build next round schedule**: Set `earliest_reveal_block` to the previous
   round's latest reveal block plus one, `latest_reveal_block` to the next start
   block, and `generation_deadline_block` from the configured generation window.
4. **Compute leader transition**:
   - `winner_hotkey: "leader"` means defense and leaves history and weight unchanged.
   - The same repository and commit as the current leader also count as defense,
     even when the winner hotkey differs.
   - A different source identity appends a leader with weight `1.0`, effective
     at the next round start block. Earlier leadership transitions remain intact.
5. **Commit atomically**: Write `state.json`, the next schedule, and changed
   `leader.json` together, using the state snapshot's Git SHA as the commit base.
   The next stage is `open`, or `paused` with `PAUSE_ON_STAGE_END=true`. If the
   competition has ended, use `finished`, retain the current round number, and
   omit the next schedule.

## Winner and leader contract

Judge-service identifies the winner of the closed-source model competition as:

```json
{
  "winner_hotkey": "miner_hotkey",
  "repo": "miner/repo",
  "commit": "0123456789abcdef0123456789abcdef01234567"
}
```

`repo` and the exact commit identify the winning Python application. A new leader
contains those fields plus `hotkey`, `weight`, and `effective_block`. No miner
Docker image, build result, or GPU selection is required. The generated procedural
Three.js output contract is unchanged.

For legacy weight setters, `leader.json` retains a `docker` string, defaulting
to `""` for leaders in this competition. Older readers require it to load the
history. Weight calculation uses only hotkey, weight, and effective block.
Existing Docker strings in historical entries are retained. `hardware` can be
omitted because the legacy reader supplies a default. The compatibility field
is unused for Python generation and does not require a miner image build.

Round-manager trusts the finalized winner from the judge. Quality judging,
independent source audit, transcript replay validation, and operator live
baseline/incumbent generation remain separate service responsibilities. The
single passed/failed validation outcome belongs to the judge/validator contract;
round-manager does not rerun it.

## State files

Reads and writes to the competition Git repository:

| File | R/W | Description |
|------|-----|-------------|
| `state.json` | R/W | Current round, stage, and next-stage ETA |
| `config.json` | R | Competition dates and timing |
| `leader.json` | R/W | Append-only leader transition history |
| `rounds/{n}/schedule.json` | R/W | Reveal and generation deadline blocks |
| `rounds/{n}/winner.json` | R | Finalized winner's hotkey, repository, and commit |

There is no `builds.json` dependency in this service.

## Development

```bash
poetry install
poetry poe test
poetry poe lint
```

## Running

Configure `GITHUB_TOKEN`, `GITHUB_REPO`, and optionally `GITHUB_BRANCH` (default
`main`). Existing timing, network, pause, and notification settings are unchanged;
there are no miner build or hardware settings.

```bash
poetry run python main.py
```

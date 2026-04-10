# Generation Orchestrator

Regenerates miner submissions on serverless GPUs so the judge service can verify them. The orchestrator does **not** decide pass/reject — it only produces fresh outputs for the judge to compare against.

## Algorithm

1. **Leader generation**: Generate baseline 3D models using the current leader's Docker image. Runs continuously, safe to restart (skips completed prompts).

2. **Build tracking**: Monitor GitHub Actions for miner Docker builds. Track build status (pending → success/failure) and notify when images are ready.

3. **Audit processing**: When judge-service writes a miner's hotkey into `require_audit.json`:
   - Wait for the Docker build to complete.
   - Deploy the miner's image to a serverless GPU (Targon / Verda).
   - Regenerate all submitted prompts using the same seed and prompt set.
   - Render PNG previews into the same view bundle layout the judge consumes.
   - Hand off to the judge: the orchestrator writes `generated.json` and marks the report `completed`. The judge then runs a `submitted` vs `generated` duel and writes the final verdict to `generation_audits.json`. See `judge-service/README.md` for the duel logic and the 0% pass margin.

4. **Multi-pod orchestration**: For each miner, deploy multiple GPU pods concurrently:
   - Start with initial pods, expand to the target count.
   - Coordinator assigns prompts with per-pod locking and retries.
   - Track failures per pod, replace bad pods.
   - Cleanup pods after completion.

5. **Generation time as a hard gate**: Independently of the duel, the orchestrator may reject an audit before forwarding it to the judge if the miner's pipeline cannot meet the round's total generation-time budget. The exact policy is still being finalized — under consideration:
   - **Strict:** any miner whose total generation time exceeds the budget is rejected outright, regardless of how their outputs compare in the duel.
   - **Lenient:** only the batches that fit within the budget are accepted; prompts in over-time batches are dropped (and so count as missing in the verification duel).

   In either case, the time check is the only orchestrator-side accept/reject decision; everything else flows through the judge.

### What the orchestrator no longer does (deprecated)

A previous version had the orchestrator compute DINOv3 distances between submitted and regenerated outputs and pass/reject the miner based on a "match rate" plus generation time. That path is gone: individual generations are not deterministic, so per-prompt distance metrics were too fragile to base accept/reject on. The verdict now comes from the judge service running the same multi-stage VLM duel it uses for the tournament — see `judge-service/README.md`.

## State Files

Reads and writes to the competition Git repository:

| File | R/W | Description |
|------|-----|-------------|
| `state.json` | R | Current round and stage |
| `config.json` | R | Competition configuration |
| `leader.json` | R | Current leader's Docker image |
| `rounds/{n}/seed.json` | R | Random seed for generation |
| `rounds/{n}/prompts.txt` | R | Prompt URLs for the round |
| `rounds/{n}/submissions.json` | R | Miner submissions |
| `rounds/{n}/require_audit.json` | R | Miners the judge has asked to be regenerated |
| `rounds/{n}/builds.json` | W | Docker build status per miner |
| `rounds/{n}/generation_reports.json` | W | Per-miner regeneration status (`completed` / `rejected` / generation-time stats). The actual audit verdict is written by the judge to `generation_audits.json`. |
| `rounds/{n}/{hotkey}/generated.json` | W | Regenerated GLB / PNG locations consumed by the judge's verification duel |
| `rounds/{n}/{hotkey}/pod_stats.json` | W | Per-pod generation stats and termination reasons |

## GPU Providers

Supports multiple serverless GPU providers:

- **Targon**: Default provider
- **Verda**: Optional backup

Configure via `GPU_PROVIDERS` env var (e.g., `"targon,verda"` for Targon-first with Verda fallback).

## Development

```bash
poetry install
poetry poe lint
```

## Running

```bash
poetry run python -m generation_orchestrator.main
```
# Generation Orchestrator

Verifies miner submissions by regenerating 3D outputs using miner Docker images on serverless GPUs.

## Algorithm

1. **Leader generation**: Generate baseline 3D models using the current leader's Docker image. Runs continuously, safe to restart (skips completed prompts).

2. **Build tracking**: Monitor GitHub Actions for miner Docker builds. Track build status (pending → success/failure) and notify when images are ready.

3. **Audit processing**: When judge-service requests verification for a miner:
   - Wait for Docker build to complete
   - Deploy miner's image to serverless GPU (Targon/Verda)
   - Regenerate all submitted prompts using the same seed
   - Render PNG previews
   - Compare regenerated outputs to submitted ones (DINOv3 distance)
   - Pass/reject based on match rate and generation time

4. **Multi-pod orchestration**: For each miner, deploy multiple GPU pods concurrently:
   - Start with initial pods, expand to the target count
   - Work-stealing queue distributes prompts across pods
   - Track failures per pod, replace bad pods
   - Cleanup pods after completion

5. **Audit verdict**: Pass if:
   - Trimmed median generation time ≤ limit
   - Non-critical mismatches ≤ tolerance
   - Critical prompt mismatches ≤ tolerance (stricter)

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
| `rounds/{n}/require_audit.json` | R | Miners needing verification |
| `rounds/{n}/builds.json` | W | Docker build status per miner |
| `rounds/{n}/generation_audits.json` | W | Audit results (pass/reject) |
| `rounds/{n}/{hotkey}/generated.json` | W | Regenerated GLB/PNG locations |

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
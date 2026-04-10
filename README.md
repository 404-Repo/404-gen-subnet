# 404-GEN Competition System

A decentralized 3D content generation competition running on Bittensor Subnet 17.
Miners submit open-source solutions for AI-powered 3D model generation, and validators run transparent competitions to determine the best solution.

## Competitions

Miners compete by providing the best 3D generation solution. Competition descriptions are in the `Competitions/` folder.

## Competition Mechanics

The competition follows a **winner-stays-in** format with a **proof-of-work model**:

1. **Miners generate**: When a round opens, miners use published seed and prompts to generate 3D models and upload to their CDNs
2. **Validators collect**: Submissions are downloaded and rendered for comparison
3. **VLM judges**: A vision-language model compares outputs in pairwise duels
4. **Validators audit**: Winners are verified by regenerating their outputs using the miner's Docker image on serverless GPUs

All competition state is stored in a public git repository, making every decision auditable and every transition traceable.

## Stage Machine

The competition progresses through well-defined stages:

| Stage | Owner | Description |
|-------|-------|-------------|
| OPEN | submission-collector | Submission window open, miners register on-chain |
| MINER_GENERATION | submission-collector | Seed published, miners generate and upload 3D files |
| DOWNLOADING | submission-collector | Fetching 3D files from miner CDNs, rendering previews |
| DUELS | judge-service | VLM-based pairwise comparisons, verification requests |
| FINALIZING | round-manager | Updating the leader and creating the next round schedule |
| FINISHED | — | Competition complete, no further rounds |
| PAUSED | — | Manual hold for inspection or intervention |

```
FINALIZING ──► OPEN ──► MINER_GENERATION ──► DOWNLOADING ──► DUELS ──► FINALIZING
     │                                                                      │
     ▼                                                                      │
  FINISHED ◄────────────────────────────────────────────────────────────────┘
```

## Services

| Service | Description |
|---------|-------------|
| `submission-collector` | Monitors the round schedule, collects miner submissions, downloads 3D files, renders previews. Owns OPEN, MINER_GENERATION, and DOWNLOADING stages. |
| `generation-orchestrator` | Verifies miner outputs by regenerating them using miner Docker images on serverless GPUs. Also generates baseline outputs using the leader's image. |
| `render-service-js` | Renders miner Three.js submissions to multi-view PNG images via headless Chromium with WebGL. |
| `miner-reference` | Reference miner implementation: specifications (API, output, runtime), a conformance validator, example fixtures, and a reference pod-side service. |
| `image-distance-service` | Computes perceptual distance between images using DINOv3 embeddings. Used to verify regenerated outputs match submissions. |
| `judge-service` | Runs VLM-based pairwise duels, requests verification for winners, selects the round winner. Owns the DUELS stage. |
| `round-manager` | Updates the global leader, creates new rounds with schedules, decides when the competition ends. Owns the FINALIZING stage. |
| `vllm` | External vLLM instance hosting the vision-language model for pairwise comparisons. |

## State Files

All state is stored in the competition git repository.

### Global State

| File | Writer | Description |
|------|--------|-------------|
| `state.json` | All stage owners | Current round number and stage |
| `config.json` | — | Competition configuration (dates, timing, thresholds) |
| `leader.json` | round-manager | Leader transition history with weights |
| `prompts.txt` | — | Global prompt pool |

### Per-Round State (`rounds/{round_number}/`)

| File | Writer | Description |
|------|--------|-------------|
| `schedule.json` | round-manager | Block window (earliest/latest reveal, generation deadline) |
| `seed.json` | submission-collector | Random seed for deterministic generation |
| `prompts.txt` | submission-collector | Selected prompts for the round |
| `submissions.json` | submission-collector | Miner submissions from chain |
| `builds.json` | generation-orchestrator | Docker build status per miner |
| `require_audit.json` | judge-service | Miners requiring output verification |
| `generation_reports.json` | generation-orchestrator | Generation reports (completed/rejected) |
| `matches_matrix.csv` | judge-service | All match results (margin values) |
| `winner.json` | judge-service | Final round winner |

### Per-Miner State (`rounds/{round_number}/{hotkey}/`)

| File | Writer | Description |
|------|--------|-------------|
| `submitted.json` | submission-collector | Downloaded submission locations |
| `generated.json` | generation-orchestrator | Regenerated output locations for verification |
| `duels_*.json` | judge-service | Detailed match reports with per-prompt outcomes |

## Verification Pipeline

When a miner becomes a candidate winner, they are sent for verification:

1. **Build tracking**: Monitor GitHub Actions for the miner's Docker image build
2. **Pod deployment**: Deploy the image on a 4×H200 GPU pod via the batch generation API
3. **Regeneration**: Send prompts in sequential batches (128 prompts, 4 batches of 32) with the same seed used in the round
4. **Validation & rendering**: Each returned JavaScript module is statically analyzed, executed in a sandbox, and rendered via headless Chromium
5. **Comparison**: Rendered outputs compared to submissions via VLM-based judging
6. **Verdict**: Pass if regenerated outputs match submission quality

If verification fails, the timeline is discarded and alternative winners are evaluated.

## Leader Transitions

The `leader.json` file tracks leadership history:

```json
{
  "transitions": [
    {
      "hotkey": "5ABC123...",
      "effective_block": 12345,
      "weight": 1.0
    },
    {
      "hotkey": "5DEF456...",
      "effective_block": 12500,
      "weight": 1.0
    }
  ]
}
```

When a leader successfully defends, their weight decays (down to a floor). New winners start with weight 1.0.

## Storage

- **Git repository**: All state files and competition history
- **R2 (Cloudflare)**: Miner submissions and rendered PNG previews

## GPU Providers

The generation-orchestrator supports multiple serverless GPU providers:

- **Targon**: Default provider
- **Verda**: Optional backup

Configure via `GPU_PROVIDERS` env var (e.g., `"targon,verda"` for Targon-first with Verda fallback).

## Development

Each service uses Poetry for dependency management:

```bash
cd <service-directory>
poetry install
poetry poe lint
```

## License

The provided code is MIT-licensed (see [LICENSE](./LICENSE)).

The deployed system additionally depends on third-party AI models with their own
licenses — most notably **DINOv3** (Meta, gated, requires accepting Meta's license
terms and supplying `HF_TOKEN`) and **GLM-4.6V-Flash** (Zhipu AI / Z.ai). See
[NOTICE.md](./NOTICE.md) for attributions, citations, and operator obligations.
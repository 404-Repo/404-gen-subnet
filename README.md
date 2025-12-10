# 404-GEN Competition System

A decentralized 3D content generation competition running on Bittensor Subnet 17. 
Miners submit open-source solutions for AI-powered 3D model generation, and validators run transparent competitions 
to determine the best solution.

## Competitions

Miners will compete by providing the best Solution. Descriptions of the competitions can be found in folder "Competitions".

## Competition Mechanics

The competition follows a **winner-stays-in** format. A defending champion holds the leader position, 
and challengers attempt to dethrone them each round. When multiple miners submit their solution in the same round, 
they compete against the current leader with their solution sequentially by submission time. If a challenger wins, they become the 
new leader and subsequent challengers must beat them.

All competition state is stored in a public git repository, making every decision auditable and every transition traceable.

## Stage Machine

The competition progresses through well-defined stages, each owned by a single service:

| Stage | Owner | Description |
|-------|-------|-------------|
| COLLECTING | submission-collector | Gathering miner submissions within the block window |
| GENERATING | generation-orchestrator | Building containers, deploying models, generating 3D outputs, rendering previews |
| DUELS | judge-service | Comparing generated outputs to determine the round winner |
| FINALIZING | round-manager | Updating the leader and creating the next round schedule |
| FINISHED | — | Competition complete, no further rounds |
| PAUSED | — | Manual hold for inspection or intervention |

## Services

| Service | Description |
|---------|-------------|
| `submission-collector` | Monitors the round schedule, collects miner submissions within the block window. Owns the COLLECTING stage. |
| `generation-orchestrator` | Coordinates the generation pipeline: waits for container builds, deploys models, runs generation, triggers rendering, and stores results. Owns the GENERATING stage. |
| `render-service` | Standalone GPU service that converts PLY files to PNG renders. Called by `generation-orchestrator`. |
| `judge-service` | Compares generation results using a vision-language model, selects the round winner. Owns the DUELS stage. |
| `round-manager` | Manages competition lifecycle: updates the global leader, creates new rounds with schedules, and decides when the competition ends. Owns the FINALIZING stage. |
| `vllm` | Hosts the VLM used by judge-service for pairwise comparisons. |

## State Files

All state is stored in the competition git repository under a deterministic structure.

### Global State

| File | Writer | Description |
|------|--------|-------------|
| `state.json` | All stage owners | Current round number and stage |
| `leader.json` | round-manager | Active leader and pending leader transition |

### Per-Round State (`rounds/{round_number}/`)

| File | Writer | Stage | Description                                                   |
|------|--------|-------|---------------------------------------------------------------|
| `schedule.json` | round-manager | FINALIZING | Block window for submissions (earliest/latest reveal blocks)  |
| `submission.json` | submission-collector | COLLECTING | Collected miner submissions with timestamps                   |
| `seed.json` | generation-orchestrator | GENERATING | Randomly selected seed for deterministic generation and duels |
| `prompts.txt` | generation-orchestrator | GENERATING | Image prompts for 3D generation                               |
| `builds.json` | generation-orchestrator | GENERATING | Container build status per miner                              |
| `{hotkey}/generations.json` | generation-orchestrator | GENERATING | Generation results and R2 references                          |
| `{hotkey}/duels.json` | judge-service | DUELS | Duel outcomes for this miner                                  |
| `judge_progress.json` | judge-service | DUELS | Progress tracking for sequential duels                        |

### Leader Transition

The `leader.json` file tracks both the current leader and any pending transition:

```json
{
  "active": {
    "hotkey": "5ABC123...",
    "since_block": 12345
  },
  "pending": {
    "hotkey": "5DEF456...",
    "effective_block": 12500
  }
}
```

New leaders are determined at the end of DUELS but become active at the start of the next round. This scheduled transition ensures fairness — all participants know in advance when leadership changes take effect.

## Transparency

Every aspect of the competition is designed for public verification:

- **Git-based state**: All decisions are recorded as commits with full history
- **Open source**: Competition logic, judging prompts, and evaluation criteria are public
- **Deterministic replay**: Seeds and prompts are stored, allowing independent reproduction of results
- **Staged transitions**: Leadership changes are announced before they take effect

Validators can independently verify competition outcomes by examining the public state repository and replaying any contested decisions.

## Stage Transitions

```
FINALIZING ──► COLLECTING ──► GENERATING ──► DUELS ──► FINALIZING
     │                                                      │
     │                                                      │
     ▼                                                      │
  FINISHED ◄────────────────────────────────────────────────┘
```

## Storage

- **Git repository**: All state files and competition history
- **R2 (Cloudflare)**: PLY files and rendered PNG previews

## License
The provided code is MIT License compatible.

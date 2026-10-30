# 404-GEN Competition System

A decentralized 3D content generation competition running on Bittensor Subnet 17.
Miners submit open-source solutions for AI-powered 3D model generation, and validators run transparent competitions to determine the best solution.

For miner installation, authentication, generation and final submission, follow the
[launch quickstart](miner-reference/README.md#launch-quickstart).

## Competitions

Miners compete by providing the best 3D generation solution. Competition descriptions are in the `Competitions/` folder.

## Competition Mechanics

The competition follows a **winner-stays-in** format with a **proof-of-work model**:

1. **Miners submit source** before prompts are published, then run that Python source on the published prompts through the witness using their own OpenAI credentials and upload the existing Three.js outputs.
2. **Validators collect**: Submissions are downloaded and rendered into one canonical result bundle per miner.
3. **VLM judges**: A vision-language model compares miners' outputs in pairwise tournament duels.
4. **Validators replay**: The judge requests one validation pass per candidate. A separate validator replays its source against recorded requests/responses and publishes one pass/fail verdict. Independent source audit is also required for eligibility.

Before judging each round, the operator must generate the previous leader's
results from its pinned Python source and the published prompts/seed, then publish
`rounds/{n}/leader/generated.json`. See [required producers](shared/README.md#required-producers).

All competition state is stored in a public git repository, making every decision auditable and every transition traceable.

## Stage Machine

The competition progresses through well-defined stages:

| Stage | Owner | Description |
|-------|-------|-------------|
| OPEN | submission-collector | Submission window open, miners register on-chain |
| MINER_GENERATION | submission-collector | Seed published, miners generate and upload 3D files |
| COLLECTING | submission-collector | Collecting generated and submitted results |
| DUELS | judge-service | VLM-based pairwise comparisons, verification requests |
| FINALIZING | round-manager | Updating the leader and creating the next round schedule |
| FINISHED | — | Competition complete, no further rounds |
| PAUSED | — | Manual hold for inspection or intervention |

```
FINALIZING ──► OPEN ──► MINER_GENERATION ──► COLLECTING ───► DUELS ──► FINALIZING
     │                                                                      │
     ▼                                                                      │
  FINISHED ◄────────────────────────────────────────────────────────────────┘
```

## Services

| Service | Description |
|---------|-------------|
| `submission-collector` | Monitors the round schedule, collects miner submissions, and records generated and submitted results. Owns OPEN, MINER_GENERATION, and COLLECTING stages. |
| `render-service-js` | Renders miner Three.js submissions to multi-view PNG images via headless Chromium with WebGL. |
| `miner-reference` | Miner guide, token budgets, a Three.js validator, examples and local tools. |
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
| `validation_requests.json` | judge-service | One validation request per candidate hotkey |
| `validation_results.json` | validator | One terminal passed/failed result per candidate; absent while pending |
| `source_audit.json` | source auditor | Independent source-level verdict per miner |
| `matches_matrix.csv` | judge-service | All match results (margin values) |
| `winner.json` | judge-service | Final round winner |

### Per-Miner State (`rounds/{round_number}/{hotkey}/`)

| File | Writer | Description |
|------|--------|-------------|
| `generated.json` | submission-collector | Miner's single Three.js result and rendered-view bundle |
| `generated.json` (under `leader/`) | leader generator | Previous leader's single result bundle for the round's prompts |
| `duels_*.json` | judge-service | Detailed match reports with per-prompt outcomes |

## Verification Pipeline

When a miner becomes a candidate winner, they are sent for verification:

1. **Request**: Judge appends the candidate hotkey to the round's `validation_requests.json` channel, once per hotkey.
2. **Replay**: Validator runs the submitted Python source with the same prompts and seed, matching its outgoing requests to recorded witness evidence and returning the recorded responses. It checks outputs and budgets without new provider generations.
3. **Validation verdict**: Validator publishes `passed` or `failed` with a useful failure reason in `validation_results.json`. In-progress work has no terminal result.
4. **Source audit**: The existing independent auditor publishes `source_audit.json`; its contract is unchanged.
5. **Eligibility**: Both checks must pass for every leader in the surviving timeline. Missing results mean wait; either failure removes that candidate and the judge recomputes the timeline from cached miner matches.

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

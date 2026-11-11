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

## Operator generation

The shared [operator command](miner-runtime/run.py) runs an unchanged miner whose
root `main.py` exports `async generate(image, seed, witness)`. Use the miner's
pinned Python environment with witness-client 0.3.x, renderer-client 0.1.5 and
miner-runtime 0.1.6 installed. See the [launch quickstart](miner-reference/README.md#launch-quickstart).
The command loads the batch runner and result schema from this checkout.

```sh
set -o pipefail
WITNESS_PROVER_CONTEXTS=4 GOMAXPROCS=8 GOMEMLIMIT=2048MiB \
.venv/bin/python -u /path/to/404-subnet/miner-runtime/run.py \
  --source /path/to/leader \
  --competition-branch main \
  --proof-parameters /path/to/proof-parameters \
  --wallet-name validator --wallet-hotkey default --wallet-path ~/.bittensor/wallets \
  --workers 24 --parallel-calls 32 --parallel-preparations 4 \
  --output /path/to/competition/rounds/3/leader/generated.json \
  2>&1 | tee /path/to/generation.log
```

The command resolves `--competition-branch` (default `main`) in
`404-Repo/404-active-competition` to one commit. It reads `state.json`, `config.json`
and that round's prompts, seed and schedule at that commit. Relative prompt paths
resolve against the same commit; absolute prompt URLs are used as published.
`GITHUB_TOKEN` is optional for reading this public repository.

The wallet authenticates to `--witness-url` (default `https://witness.subnet404.xyz`),
which must serve the selected competition branch. Its saved artifacts stay under
that wallet's hotkey. After the batch, the command reads the round's saved index
from `--cdn` (default `https://subnet404.xyz`) using the schedule's storage prefix.
It validates the index and writes `--output` with absolute artifact URLs. Recorded
failures remain recorded failures. Incomplete batches or invalid indexes exit
with an error before writing the output.

`--workers` controls active prompts; `--parallel-calls` controls active model
captures across those prompts. Match the call limit to the Pod's configured
capacity. The local thread pool has one thread per prompt plus one per model
slot, allowing threaded miners and SDK preparation to progress together.
`--parallel-preparations` bounds TLS setup, credential proving and authorization
(default three). Set `WITNESS_PROVER_CONTEXTS=4` before starting Python and use
`--parallel-preparations 4` to allow four concurrent native proofs. The contexts
share the `GOMAXPROCS` CPU budget and `GOMEMLIMIT` soft memory budget.
Retrying the command reuses the batch
runner's saved-result lookup and skips completed prompts. `--attempts` defaults
to three passes over unfinished prompts. Authentication uses the local Codex login,
or `OPENAI_API_KEY` when `--api` is supplied. Logs go to stderr and the completed
output path is printed to stdout.

For the isolated two-prompt fixture, select `--competition-branch witness-test`,
`--witness-url https://witness-test.subnet404.xyz`, the admitted test wallet and a
separate local `--output` path. Test state, seed, prompts and storage scope come
from that branch.

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
| [`validation-service`](validation-service/README.md) | Schedules candidate validation on replay Pods and publishes terminal validation verdicts. |
| [`validation-worker`](validation-worker/README.md) | Replays candidate submissions on validation Pods and verifies witness evidence, outputs and budgets. |
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

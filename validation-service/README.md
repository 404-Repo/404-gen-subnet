# Validation service

Coordinates validation of candidate winners requested by judge-service. Schedules
replays on validation Pods, collects worker results and publishes a terminal
`passed` or `failed` result. Runs alongside judge-service; source auditing remains
an independent check.

The coordinator runs on the main server and dispatches candidates to replay Pods.
Each Pod contains the trusted worker and the miner sandbox. The coordinator runs
one candidate at a time. Input preparation, witness verification,
output comparison and budget checks run outside the miner sandbox.
See [validation-worker](../validation-worker/README.md) for the Pod runtime.

## Validation

Each poll resolves `GITHUB_BRANCH` to one immutable commit, then reads these files
at that same commit:

| Path | Purpose |
| --- | --- |
| `state.json` | Select the current round and its next-stage ETA |
| `rounds/{n}/validation_requests.json` | Read candidate hotkeys requested by the judge |
| `rounds/{n}/validation_results.json` | Read existing terminal results and publish validation verdicts |

Only the current round is considered, in any stage. Missing or empty requests
mean no work. Requests are deduplicated by hotkey within the round. Each request
whose hotkey has either a `passed` or `failed` result is skipped. Missing results
leave requests pending.

For each pending candidate, the service supplies the round number, hotkey and
pinned competition commit to the worker. The worker loads the submitted source
repository and commit, published prompts and seed, canonical outputs and witness
artifact namespace at that commit.

The worker replays the submitted Python source, matching outgoing requests to
recorded witness requests and returning their recorded responses. It checks the
replayed outputs against the canonical outputs and verifies competition budgets
without issuing new provider generations.

A successful validation publishes `passed`. A validation failure publishes
`failed` with a nonempty reason. The service publishes one terminal result per
round and hotkey, retaining the round's existing results when adding verdicts.
The judge reads these results together with the independent source-audit verdicts
to determine candidate eligibility.

Pending work has no entry in the terminal result map. Repeated polls reuse the
candidate's active Pod. The worker terminates its Pod after committing a terminal
checkpoint. On its next poll, the coordinator publishes finished verdicts in one
Git commit and starts the next pending candidate if the Pod slot is free. If a Pod
disappears while work is pending, a replacement resumes the checkpoint. A
candidate with exhausted prompt attempts stays pending. The coordinator removes
any remaining Pods for completed candidates or requests that leave the current
round. Each poll reads one commit. Later polls resolve the branch again to see new
requests, results or rounds.

Missing `state.json`, malformed competition data and infrastructure failures are
service errors. They are logged and retried on a later poll, leaving affected
requests pending rather than publishing a miner failure.

## Configuration and running

Use Python 3.12 and Poetry 2.2.1, matching the service development tooling.
Run commands from `validation-service/` so `.env` and the local `../shared`
dependency resolve correctly:

```bash
poetry install
```

Create a local `.env` (ignored by Git), or set the equivalent environment variables:

```dotenv
GITHUB_TOKEN=your-token
GITHUB_REPO=owner/competition-state
GITHUB_BRANCH=main
MIN_CHECK_STATE_INTERVAL=120
MAX_CHECK_STATE_INTERVAL=1800
LOG_LEVEL=DEBUG
RUNPOD_PROVIDER_API_KEY=your-runpod-key
VALIDATION_POD_IMAGE=registry.example/validation-worker@sha256:IMMUTABLE_IMAGE_DIGEST
VALIDATION_POD_REGISTRY_ID=your-runpod-registry-credential-id
VALIDATION_POD_PREFIX=validation-example
VALIDATION_WORKER_ENV='{"R2_ENDPOINT":"https://your-account.r2.cloudflarestorage.com","R2_BUCKET":"main","R2_REPLAY_BUCKET":"replay","R2_ACCESS_KEY_ID":"your-read-key","R2_SECRET_ACCESS_KEY":"your-read-secret","WITNESS_DEPLOYMENT":"your-deployment","RENDERER_ID":"original-renderer-image"}'
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `GITHUB_TOKEN` | Required | Secret GitHub token with read and write access to the competition repository |
| `GITHUB_REPO` | Required | Competition state repository as `owner/repo` |
| `GITHUB_BRANCH` | `main` | Competition branch to resolve on every poll |
| `MIN_CHECK_STATE_INTERVAL` | `120` | Minimum delay between polls, in seconds |
| `MAX_CHECK_STATE_INTERVAL` | `1800` | Maximum delay between polls, in seconds |
| `LOG_LEVEL` | `DEBUG` | Loguru logging level |

The Pod image must contain the worker and its sandbox. Reserve a distinct
`VALIDATION_POD_PREFIX` for each coordinator. It tracks its Pods by name and
uses `VALIDATION_POD_IMAGE` when launching a Pod. Each uses 4 `cpu3c` vCPUs and a
12 GB container disk. `VALIDATION_WORKER_ENV` supplies the worker's R2 and Witness
settings. Include `"VALIDATION_JOBS":"8"` in that JSON to run up to eight prompts
at once per Pod. GitHub settings come from the coordinator configuration. Set
`WITNESS_COMPETITION_BRANCH` in the worker environment when a test branch reuses
frozen evidence from another branch.

The shared wait-time helper clamps the delay until `state.next_stage_eta` between
the configured minimum and maximum. With no ETA, or after a failed poll, the
service waits the maximum interval. `SIGINT` and `SIGTERM` interrupt the wait and
stop the service; an in-flight poll completes its reads and stops between jobs.

```bash
poetry run python main.py
```

## Development

```bash
poetry run poe lint
poetry run python -m unittest discover -s tests
poetry build
```

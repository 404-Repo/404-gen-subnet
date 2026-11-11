# Leader generation service

The service stays running between rounds, maintains one dedicated ChatGPT login,
and generates the incumbent leader's baseline during `miner_generation`. It reads
competition state at one Git revision, freezes the latest `leader.json`
repository/commit and the witness's official prompt IDs, image hashes, seed and
storage scope, then submits one self-starting RunPod CPU request per attempt through
`miner_runtime.pod.pod_body`. The pod starts from a digest-pinned generation image,
verifies its source and proof artifacts, fetches the pinned leader source, and
runs generation in the foreground.

The worker creates one async witness client with the configured call and
preparation limits and explicitly requests a fresh witness pod before every pass,
including the first. Each pass executes
`generate(image, seed, witness)` once per pending prompt through
`miner_runtime.batch.run_pass`, with concurrency bounded by `WORKERS`. A miner-code
exception skips that prompt for the current pass. Transient image, render-service,
or submission errors leave prompts pending while the others continue. Returning
`None` saves the terminal `generation_failed` outcome. Final renderer validation
rejections save the terminal `render_rejected` outcome. Saved terminal outcomes
count as completed and are never regenerated. After a pass, the worker confirms
saved results before deciding whether to retry.

When prompts remain pending, the worker waits until at least 20 minutes since
its previous explicit witness request, requests a replacement through the SDK,
then retries only pending IDs. Failed explicit requests also consume this interval;
a pass lasting at least 20 minutes needs no extra wait. One configured client
remains open across passes. The SDK's automatic connection recovery and allocation
behavior operates independently of this explicit-request interval.
Each worker downloads its image from the original URL in the frozen competition
inputs and verifies the image hash before generating it.

When every official prompt has a terminal result in the witness's
`submitted.json` index, the service records completion and terminates the
generation pod. The submission collector reads the same index during
`collecting` and writes `rounds/{n}/leader/generated.json` for the judge.

## Configuration and startup

Install the [client development prerequisites](../witness-client/DEVELOPMENT.md),
then run `poetry install` and `poetry run python main.py` in this directory.
Settings load from the environment and the local `.env` file.

| Required variable | Meaning |
| --- | --- |
| `GITHUB_REPO`, `GITHUB_TOKEN` | Competition repository and token with read access |
| `GENERATION_IMAGE` | Published generation image reference pinned with `@sha256:<digest>` |
| `RUNPOD_API_KEY` | Generation CPU lifecycle credentials; retained on the service |
| `CDN_URL` | Same artifact CDN origin/prefix used by the collector |
| `LEADER_CODEX_HOME` | Dedicated durable file-backed ChatGPT login directory |
| `WALLET_PATH` | Dedicated unencrypted sr25519 operator wallet directory |

`CODEX` selects the executable, which must report `codex-cli 0.167.0`. Provision
a file-backed subscription login in `LEADER_CODEX_HOME` before startup. The service must
be that login's only active credential owner. A lock in the login directory
prevents another service instance from refreshing it with a different state
directory. A separate lock owns service state.

`WALLET_NAME=leader` and `WALLET_HOTKEY=default` select the operator hotkey.
Set its public address as `leader_generation_hotkey` in the round's
`schedule.json` to enable generation and collection for that round. The witness
must admit the operator hotkey. The incumbent miner's hotkey identifies the
source in `leader.json`. For manual generation, use the pinned source and the
same operator wallet so results go to the same witness index.
The operator hotkey and provider account are frozen per job. The coldkey stays
with the operator.

Optional settings include `GITHUB_BRANCH=main`,
`WITNESS_URL`, `SUBTENSOR_RPC_URL`,
`STATE_DIRECTORY=/var/lib/404-leader-generation`, `MAX_HOURLY_COST=0.25`,
`CPU_ID=cpu3c`, `VCPUS=8`, `DISK_GB=20`, and `DATA_CENTER`. The hourly ceiling
includes the CPU quote and a disk estimate of $0.10/GB/month divided by 730 hours.
It is not a total-spend limit.

Execution defaults are `WORKERS=24`, `PARALLEL_CALLS=32`,
`PARALLEL_PREPARATIONS=4`, `PROVER_CONTEXTS=4`, `GO_CPUS=8`,
`GO_MEMORY_MIB=2048` (`GOMAXPROCS=8`, `GOMEMLIMIT=2048MiB`). The image build pins its
Python base, Go toolchain, proof parameters and Python
dependencies. It verifies installed SDK versions, the freshly built native prover
and its proof key. The worker reads and logs the source commit from the image
metadata, checks artifact integrity and checks the actual CPU/RAM allocation.

## Isolated testing

Copy [`.env.test.example`](.env.test.example) to this directory's private `.env`.
It selects `404-Repo/404-active-competition`, branch `witness-test`, and
`https://witness-test.subnet404.xyz`. The service, gateway, and collector must
follow that branch. The collector writes `rounds/{n}/leader/generated.json` on
`witness-test`. Artifact URLs use the shared `https://subnet404.xyz` CDN with the
test round's distinct name. A second competition repository or branch is unnecessary.

The test gateway admits only signed requests from
`5CAJpeDRXoE9EnsJRsDP1MLKuP31aztpXyPH5JaR49b14P1y`. Provision that hotkey in the
configured test wallet. Keep its private key, GitHub and RunPod tokens, and the
dedicated file-backed subscription login outside Git. The example uses separate
state and login directories.
Fill in the published `GENERATION_IMAGE` digest and the required API tokens.

The competition fixture must also supply:

- `leader.json` with a leader entry pinning a Python miner repository and full
  source commit containing `main.py` and `generate(image, seed, witness)`.
- `state.json` selecting `miner_generation` and a test round number.
- That round's `prompts.txt`, `seed.json`, and `schedule.json`, with
  `leader_generation_hotkey` set to the test operator's public address,
  `witness_save_submissions=true`, and `generation_deadline_block=null` for a
  persistent test round. To test collection, supply the round's
  `submissions.json` and select `collecting` after generation completes.

The fixture uses round `witness-test-20261110-0`, two prompts and seed `42`.
Pin the latest leader entry from competition `main` in the fixture's `leader.json`.
Use a fresh test round for a clean run or to change frozen prompts or seed.
On `witness-test`, add the new round's `prompts.txt`, `seed.json`, and
`schedule.json`, then select it in `state.json`. The new round has its own saved
results and service journal. Restarting the same round reuses saved terminal results.

## Generation image builds

Run the build workflow from the repository root. A release build requires the
clean checkout's HEAD to be the agreed local release branch tip and the current
origin branch tip. Its build context comes from that exact commit.

```sh
python leader-generation-service/build_image.py --release-branch main --tag leader-generation:release
```

The command builds a local image. Publishing and deployment are separate actions.
Configure `GENERATION_IMAGE` with the published registry digest of a worker that
supports the controller's create request. It is frozen per round, so replacing a
pod continues using the same verified runtime. The source
commit is recorded in `/opt/leader-image/provenance.json` during the image build
and read by the worker at startup.

Development builds may use a feature checkout and modified tracked files. New SDK
source files must be added to Git's index before building. Untracked login files
are excluded. Development images record their purpose and cannot run service jobs.

```sh
python leader-generation-service/build_image.py --development --tag leader-generation:development
```

For offline development verification, add `--offline --wheelhouse /path/to/cached-wheels`.
Both base images must already exist locally. `--python-image` and `--go-image` can
select compatible cached development bases. Offline builds disable build networking
and package-index access. Release builds use the pinned bases and require remote
source verification. Renderer wheel builds retain their isolated setuptools backend.

## Credentials and recovery

Subscription maintenance runs independently of competition checks and CPU setup.
It renews the login before expiry, including while no round is active. Before
each pod create, the controller requires an access token valid for the
competition's configured generation window plus ten minutes. It refreshes on
the controller as needed and defers allocation if the token still expires too
soon. The RunPod create request supplies the serialized operator hotkey and
access token. The worker installs them in private files at startup. The refresh
token, Codex login, and coldkey stay on the controller.

If renewal requires signing in again, maintenance reports the error type. Repair
the same dedicated account and let the service retry. A different account or
operator hotkey cannot take over an existing job.

`STATE_DIRECTORY/rounds/{n}.json` stores frozen inputs, allocation attempts,
the completed-prompt count, last progress time, and whether the current stall was warned.
`STATE_DIRECTORY/runs/{uuid}/config.json` stores the public worker configuration.
Allocation lifecycle states are `intent`, `creating`, `active` and `terminated`.
The controller reads provider status separately.
The witness owns completed results.
Retain this directory and `LEADER_CODEX_HOME` across restarts.

Allocation intent is saved before create. An uncertain reply is reconciled
against fresh inventory; create is never blindly repeated. If inventory cannot
confirm the attempt, inspect the recorded exact name before intervening.
During generation, each iteration reads
`CDN_URL/<storage-prefix>rounds/<full-round-name>/<operator-hotkey>/submitted.json`
and logs completed, total, newly completed, and pending prompt counts. A pod stays
running while saved results advance. After 20 minutes without new terminal results,
the service sends one warning per stall to `DISCORD_WEBHOOK_URL`, if configured.
After `STALL_REPLACEMENT_SECONDS` without progress (one hour by default), it confirms
deletion of the recorded CPU pod and creates a replacement with the same frozen inputs
and identity. The replacement interval must exceed 1200 seconds. Startup counts
toward the stall window. Progress and warning state survive service restarts.
Older rounds only clean up their CPU pods. CPU disk is not needed to recover
progress. Unavailable or regressing saved results do not trigger stall replacement.
The service retries the check on the next iteration. A Discord delivery failure
does not block pod recovery.

`PROGRESS_INTERVAL_SECONDS=1200` sets the wait after every iteration, including
failures, and must be at least 1200 seconds (20 minutes). Stall thresholds are
evaluated at each check; longer intervals delay warnings and replacement.
Allocation-request timestamps in the private journal enforce the minimum gap
between generation-pod creates across rounds, restarts, and rejected requests.
Failed cleanup prevents another allocation. The service retries pod deletion on
later iterations. The collector can read completed witness results while cleanup
is pending.

State and block deadlines are rechecked after worker configuration preparation, before the
create request. A closed round starts no new attempts. Shutdown starts no new
workers; recorded pods are cleaned up on the next startup's iteration. Provider
operations already in flight finish before shutdown closes clients and releases
state ownership. `CREDENTIAL_CHECK_INTERVAL_SECONDS=30` independently bounds
credential checks.

CPU startup and worker logs are available through RunPod's pod logs. Worker files
under `/opt/leader-run` disappear on termination. Service logs contain public
round/stage progress and error types, with local-variable diagnostics disabled.

## Offline checks

```sh
poetry poe test
poetry poe lint
```

Tests replace provider, source preparation and credential-refresh I/O. They cover
idle renewal, create-time credential delivery, progress monitoring, restart
reconciliation, worker retry spacing, deadlines and pod cleanup. Shared worker tests live in
`miner-runtime/tests`. Live RunPod/Linux readiness, native proof execution,
provider authentication and throughput need separate acceptance runs.

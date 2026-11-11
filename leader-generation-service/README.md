# Leader generation service

The controller stays running between rounds, maintains one dedicated ChatGPT login,
and manages RunPod CPU pods and saved-result progress during `miner_generation`.
It reads Git for round eligibility and the prompt IDs it tracks in `submitted.json`.
Each pod receives the competition repository, branch, and expected round along
with its credentials and runtime settings.

The worker starts from a digest-pinned generation image and checks its source
and proof artifacts. It resolves the competition branch to one Git revision
and checks the round and stage there. It reads `leader.json`, `prompts.txt`,
and `seed.json` from that revision, then fetches and loads the pinned leader
commit once.

The worker creates one async witness client with the configured call and
preparation limits and explicitly requests a fresh witness pod before every pass,
including the first. Each pass executes
`generate(image, seed, witness)` once per pending prompt through
`miner_runtime.batch.run_pass`, with concurrency bounded by `WORKERS`. A miner-code
exception skips that prompt for the current pass. Transient image, render-service,
or submission errors leave prompts pending while the others continue. Returning
`None` saves the terminal `generation_failed` outcome. Final renderer validation
rejections save the terminal `render_rejected` outcome. Saved terminal outcomes
count as completed and are never regenerated. The worker reads CDN
`submitted.json` before each pass and checks it again for any successful saves
after a failed request.

When prompts remain pending, the worker waits until at least 20 minutes since
its previous explicit witness request, requests a replacement through the SDK,
then retries only pending IDs. Failed explicit requests also consume this interval;
a pass lasting at least 20 minutes needs no extra wait. One configured client
remains open across passes. The SDK's automatic connection recovery and allocation
behavior operates independently of this explicit-request interval.
Each worker downloads its prompt image from the URL published in Git.
Repository prompt paths resolve at the same revision as the prompt list.

When every official prompt has a terminal result in the witness's
`submitted.json` index, the service terminates the generation pod.
The submission collector reads the same index during
`collecting` and writes `rounds/{n}/leader/generated.json` for the judge.

## Configuration and startup

Install the [client development prerequisites](../witness-client/DEVELOPMENT.md),
then run `poetry install` and `poetry run python main.py` in this directory.
Settings load from the environment and the local `.env` file.

| Required variable | Meaning |
| --- | --- |
| `GITHUB_REPO`, `GITHUB_TOKEN` | Public competition repository and controller token with read access |
| `GENERATION_IMAGE` | Published generation image reference pinned with `@sha256:<digest>` |
| `RUNPOD_PROVIDER_API_KEY` | Generation CPU lifecycle credentials; retained on the service |
| `CDN_URL` | Artifact CDN origin/prefix used by the controller, worker, and collector |
| `LEADER_CODEX_HOME` | Dedicated durable file-backed ChatGPT login directory |
| `WALLET_PATH` | Dedicated unencrypted sr25519 operator wallet directory |

`CODEX` selects the executable, which must report `codex-cli 0.167.0`. Provision
a file-backed subscription login in `LEADER_CODEX_HOME` before startup. The service must
be that login's only active credential owner. A lock in the login directory
prevents another service instance from refreshing it. The service also takes
a process lock in `STATE_DIRECTORY`.

`WALLET_NAME=leader` and `WALLET_HOTKEY=default` select the operator hotkey.
Set its public address as `leader_generation_hotkey` in the competition's
`config.json` to enable leader generation and collection. The witness
must admit the operator hotkey. The incumbent miner's hotkey identifies the
source in `leader.json`. For manual generation, use the pinned source and the
same operator wallet so results go to the same witness index.
The coldkey stays with the operator.

Optional settings include `GITHUB_BRANCH=main`,
`WITNESS_URL`, `SUBTENSOR_RPC_URL`,
`STATE_DIRECTORY=/var/lib/404-leader-generation` (process lock directory),
`MAX_HOURLY_COST=0.25`,
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
lock and login directories. Run test and production controllers in separate
RunPod accounts if both run at the same time. Startup cleanup applies to every
`leader-runner-` pod in the configured account.
Fill in the published `GENERATION_IMAGE` digest and the required API tokens.

The competition fixture must also supply:

- `leader.json` with a leader entry pinning a Python miner repository and full
  source commit containing `main.py` and `generate(image, seed, witness)`.
- `state.json` selecting `miner_generation` and a test round number.
- `config.json` with `leader_generation_hotkey` set to the test operator's public address.
- That round's `prompts.txt`, `seed.json`, and `schedule.json`, with
  `witness_save_submissions=true`, and `generation_deadline_block=null` for a
  persistent test round. To test collection, supply the round's
  `submissions.json` and select `collecting` after generation completes.

The fixture uses round `witness-test-20261110-0`, two prompts and seed `42`.
Pin the latest leader entry from competition `main` in the fixture's `leader.json`.
Use a fresh test round for a clean run or to change frozen prompts or seed.
On `witness-test`, add the new round's `prompts.txt`, `seed.json`, and
`schedule.json`, then select it in `state.json`. The new round has its own saved
results. Restarting the same round reuses saved terminal results.

## Generation image builds

Run the build workflow from the repository root. A release build requires the
clean checkout's HEAD to be the agreed local release branch tip and the current
origin branch tip. Its build context comes from that exact commit.

```sh
python leader-generation-service/build_image.py --release-branch main --tag leader-generation:release
```

The command builds a local image. Publishing and deployment are separate actions.
Configure `GENERATION_IMAGE` with the published registry digest of a worker that
supports the controller's create request. The controller uses that digest
for each new pod. The source
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
the dedicated login and let the service retry.

At startup the controller lists RunPod pods and deletes those named with the
`leader-runner-` prefix. Failed deletions are retried from the same inventory
before any create. After cleanup, the controller immediately checks the current
round and starts a pod if the round is eligible and prompts are pending. It
tracks the current pod and stall timer in memory. During generation, each
iteration reads the current round in Git and
`CDN_URL/<storage-prefix>rounds/<full-round-name>/<operator-hotkey>/submitted.json`
and logs completed, total, newly completed, and pending prompt counts. A pod stays
running while saved results advance. After 20 minutes without new terminal results,
the service sends one warning per stall to `DISCORD_WEBHOOK_URL`, if configured.
After `STALL_REPLACEMENT_SECONDS` without progress (one hour by default), it confirms
deletion of the CPU pod and creates a replacement using `GENERATION_IMAGE`
and the current settings. The replacement interval must exceed 1200 seconds.
The stall timer starts when a pod is created and resets when saved results advance.
On a round change, the controller terminates its previous pod and reads the new
round's index. Unavailable or regressing saved results do not trigger stall replacement.
The service retries the check on the next iteration. A Discord delivery failure
does not block pod recovery.

`PROGRESS_INTERVAL_SECONDS=1200` sets the wait after every iteration, including
failures, and must be at least 1200 seconds (20 minutes). Stall thresholds are
evaluated at each check, so longer intervals delay warnings and replacement.
Failed cleanup prevents another allocation and is retried on later iterations.
After an unconfirmed create, the controller waits one interval and exits so
PM2 can restart it and run startup cleanup.

The controller rechecks eligibility and the block deadline after loading
credentials. In-flight RunPod operations finish during shutdown. The next
startup cleans up any remaining generation pods.
`CREDENTIAL_CHECK_INTERVAL_SECONDS=30` independently bounds credential checks.

CPU startup and worker logs are available through RunPod's pod logs. Worker files
under `/opt/leader-run` disappear on termination. Service logs contain public
round/stage progress and error types, with local-variable diagnostics disabled.

## Offline checks

```sh
poetry poe test
poetry poe lint
```

Tests replace provider, source preparation and credential-refresh I/O. They cover
idle renewal, create-time credential delivery, progress monitoring, startup
cleanup, worker retry spacing, and deadlines. Shared worker tests live in
`miner-runtime/tests`. Live RunPod/Linux readiness, native proof execution,
provider authentication and throughput need separate acceptance runs.

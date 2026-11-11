# Validation worker

Supports automatic validation jobs and [manual replays](MANUAL_REPLAY.md).
Manual replay runs with `poetry run python replay.py` and keeps its own local
reports. It uses a local Python subprocess with the operator's filesystem and
network access; the NsJail isolation below applies to automatic validation.

Automatic validation checks one candidate on a dedicated replay Pod. The trusted
worker reads GitHub and R2, verifies captured provider evidence, checks replayed JavaScript and
per-prompt budgets, and checkpoints progress in Git. Submitted imports and
`generate()` run in a fresh NsJail process for each replay trial. The jail filesystem is
prepared once for the validation job and reused across prompts and operational
retries; each process receives a clean scratch directory and the current image.
[validation-service](../validation-service/README.md) coordinates candidates and
publishes their terminal results in `rounds/{n}/validation_results.json`.

## Resumption

The checkpoint is `rounds/{n}/{hotkey}/validation.json`:

```json
{
  "ref": "original_40_character_input_commit",
  "prompts": {
    "prompt_id": {
      "status": "pending",
      "attempts": 1,
      "retryable": false,
      "reason": "RenderUnavailable: Missing preview recovery exhausted after 32 trials",
      "charges": {"logical_call_id": "2000000"},
      "consumed_calls": 1,
      "consumed_credits": "2000000"
    }
  }
}
```

The worker resumes at the checkpoint's original `ref`, including its submission,
prompts, seed and canonical output references. Progress commits advance the
competition branch without changing these inputs.

Each prompt has up to three recorded attempts, with delays of one second and then
two seconds. The watchdog starts with the first attempt on each Pod and covers
source preparation, sandbox startup, evidence reads, verification and execution
across that Pod's attempts. A replacement resumes from the saved attempt count.
Transient failures exhaust into `pending`, with the last error recorded. Invalid
evidence or configuration stays pending without retries. A confirmed miner or
budget failure is `failed`.

`charges` records distinct consumed logical calls and their exact decimal credit
costs. Each completed attempt queues its status, attempt count, reason and charges
through `GitBatcher`. Repeating a logical call counts once. Call and credit
totals are derived from the ledger.
A known budget breach remains a failure even if a later infrastructure operation
fails.

Writes follow GitBatcher's normal batching interval. Normal completion or a
graceful stop makes one final save. If it fails, the worker keeps its queued
results and retries, with up to three job attempts and 1s/2s delays. A replacement
worker restores the last committed snapshot and reruns unfinished work. A crash
can lose unsaved attempt counts and charges. Saved exhausted attempts remain pending until
an operator resets them. Git outages are logged while persistence is unavailable.
R2 SDK retries are disabled in the automatic entry point so prompt retries have
one owner.

`SIGTERM` and `SIGINT` stop between prompts or interrupt retry waits. Cancellation
terminates the active sandbox. Operators can explicitly reset a pending prompt's
retryable flag and attempt count after repairing infrastructure.

The Pod runs up to `VALIDATION_JOBS` prompts concurrently for one candidate.
Each slot reuses its own jail, starts a fresh miner process for each prompt and
queues progress through the same GitBatcher.

## Inputs and R2 reads

The worker reads the candidate's `submissions.json`, `prompts.txt`, `seed.json`,
`schedule.json` and `generated.json` at the checkpoint's pinned commit. It retrieves
the frozen round record and hash-checks each prompt image when that prompt starts.
Submitted source is fetched at its recorded commit and unpacked as regular files;
submitted links and unsafe archive paths are rejected. No submitted install hooks
are executed by the trusted worker. A GitHub 404 for the submitted source archive
ends the job with a failed verdict and records the repository and commit.

R2 reads happen on demand:

- `R2_BUCKET`: frozen round metadata, current prompt bytes, exact call records and pricing.
- `R2_REPLAY_BUCKET`: capture files for a requested model call and saved preview PNGs or rejections for a render request.
- The current prompt's canonical JavaScript is fetched from its published URL.

There is no whole-candidate artifact download or evidence listing. Calls are
matched by their existing request identity. The trusted worker rechecks capture
bindings and runs the maintained native Witness TLS verifier before returning a
recorded response. Saved previews use request-keyed `renders/v2/`, with `v1/`
lookup using the original `RENDERER_ID`. For a missing preview, the worker uses
the manual replay search to try connection errors, timeouts and HTTP failures
within the prompt's time limit. An absent model-call record gives the miner the
SDK connection error used by manual replay and consumes zero calls and credits.
A replay using assumed errors must reproduce the exact output with verified
recorded calls, the saved final grid when the miner returns JavaScript, and a
passing trial budget. The checkpoint records the assumptions and the accepted
trial's charges. An inconclusive replay stays pending.

The worker requires exact JavaScript correspondence and applies the shared
`PromptBudget` to the verified model calls consumed by that prompt, using the
deployment's current pricing table. Pricing updates are managed by the operator;
capture pricing hashes are informational. Temporary source, jail and capture
files are kept in private directories beneath `--output` and removed when the job
exits. Git holds the durable checkpoint.

## Miner isolation

NsJail 3.6 runs in standalone once mode with namespace creation disabled, using a
chroot, no supplementary groups, no capabilities, `no_new_privs` and the bundled
seccomp allowlist. This profile works with RunPod's default container capabilities.
It requires a root worker able to chroot and change UID/GID. Each slot uses its
own reserved UID/GID, starting at 40000.

The worker copies only the pristine runtime, submitted source and credential-free
entry point into each slot's jail. Runtime and source files are owned by root and
unwritable by the miner. Before each prompt, the worker replaces the prompt image
and recreates `/scratch` for the slot's UID. It clears the miner environment and closes
all inherited file descriptors except stdin/stdout/stderr. Worker storage,
expected outputs, raw evidence, tokens, home directories, SSH keys and agent
sockets stay outside the chroot. `/proc` and `/sys` are not mounted.

The seccomp policy denies networking, namespace and mount operations, tracing,
process-memory access and process-group/session changes. A private Unix socket
pair is allowed for asyncio. Every prompt starts a new process group; the worker
kills the group before reusing the jail or on cancellation. The process uses
normal CPU priority, a 4 GiB virtual-address-space limit, a 16 MiB per-file size limit,
128 file descriptors, 128 processes/threads and disabled core dumps. The address-space
limit is not a Pod RAM allocation.

Before Python starts, the worker sets `PYTHONHASHSEED=0`,
`OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1` and `MKL_NUM_THREADS=1`.
Python uses safe-path mode and disables the user site while honoring these trusted
startup variables. The cleared environment has no submitted `PYTHONPATH` or user
startup configuration.

The only worker connection is a bounded stdin/stdout replay protocol for recorded
model responses, saved preview outcomes and the generated output. The worker owns
all verification, accounting and Git writes. Submitted Python is never imported
by the credentialed worker. Runtime dependencies must already be present in the
pristine sandbox filesystem.

The miner shares the Pod's kernel. A missing runtime, policy or required isolation
operation leaves validation pending; there is no unisolated execution fallback.

## Configuration and running

Use Python 3.12 and Poetry 2.2.1 for the worker. Run from `validation-worker/`:

```bash
poetry install
```

The trusted runtime also needs NsJail 3.6, Node and the existing Witness native
verifier, its Node dependencies, `privateauth` binary and proof parameters. Reuse
the [Witness worker runtime setup](../witness-server/worker/README.md).
Build NsJail in the trusted Linux runtime:

```bash
apt-get update
apt-get install -y autoconf bison flex gcc g++ git libprotobuf-dev \
  libnl-route-3-dev libtool make pkg-config protobuf-compiler
git clone --branch 3.6 --depth 1 --recurse-submodules --shallow-submodules \
  https://github.com/google/nsjail.git /opt/nsjail
test "$(git -C /opt/nsjail rev-parse HEAD)" = f78475530b46d0186111a9096b30725f816b55fe
test "$(git -C /opt/nsjail/kafel rev-parse HEAD)" = 76d0f41bf3eb5c4008713d64b9767b461a9129a3
make -C /opt/nsjail -j2
install -m 755 /opt/nsjail/nsjail /usr/local/bin/nsjail
```

```dotenv
GITHUB_TOKEN=your-competition-write-and-source-read-token
GITHUB_REPO=owner/competition-state
GITHUB_BRANCH=main
WITNESS_DEPLOYMENT=competition
WITNESS_PROVIDER=codex
R2_ENDPOINT=https://your-account.r2.cloudflarestorage.com
R2_BUCKET=your-main-bucket
R2_REPLAY_BUCKET=your-replay-bucket
R2_ACCESS_KEY_ID=your-read-access-key
R2_SECRET_ACCESS_KEY=your-read-secret-key
RENDERER_ID=original-witness-image-identity
VALIDATION_JOBS=1
SANDBOX_ROOT=/opt/validation-miner-root
SANDBOX_TIMEOUT=1800
LOG_LEVEL=DEBUG
```

GitHub access must allow reading source and writing the competition checkpoint.
R2 credentials need object reads from both buckets. They remain in the trusted
worker. `WITNESS_PROVIDER` selects the original `api` or `codex` request
normalization and defaults to `codex`. `VALIDATION_JOBS` accepts 1 through 8
and defaults to 1. `SANDBOX_TIMEOUT` sets the watchdog in seconds for a prompt's
attempts on one Pod.

```bash
poetry run python main.py --round-num 7 --hotkey MINER_HOTKEY \
  --ref COMPETITION_COMMIT_SHA --output /private/validation/candidate
```

## Round runtime

`Dockerfile.sandbox` pins Python **3.13.18** and its Linux x86_64 base-image digest.
[requirements-sandbox.txt](requirements-sandbox.txt) pins every direct and transitive
package, including NumPy **2.5.4** and Pillow **13.0.0**, from the miner runtime's
published direct requirements plus the runner's aiofiles/loguru dependencies.
Installation uses only binary wheels, installs no additional dependencies and runs
`pip check`. Bundled witness/renderer client versions and source hashes are recorded
alongside Python's exact build and all installed package versions in `/runtime.json`.

For a local development build from the repository root:

```bash
docker buildx build --platform linux/amd64 --load \
  -f validation-worker/Dockerfile.sandbox -t validation-miner:dev .
```

Publish the image digest and version metadata as `rounds/{n}/runtime.json`
in the competition repository:

```json
{
  "image": "registry.example/validation-miner@sha256:64_hex_manifest_digest",
  "environment": {"python": "3.13.18", "packages": {}, "clients": {}, "sdk_files": {}}
}
```

`environment` is the complete `/runtime.json` from the built image, including its
Python build, platform and base image. Use the registry manifest digest. The
metadata records the sandbox used for automatic validation of that round.

Production publication must use clean, committed source integrated into `main` and
pushed to its remote. From that exact source at the repository root:

```bash
git fetch origin main
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
test -z "$(git status --porcelain)"
source_sha="$(git rev-parse HEAD)"
release_context="$(mktemp -d)"
git archive "$source_sha" | tar -x -C "$release_context"
sandbox_repository=YOUR_REGISTRY/validation-miner
sandbox_tag="$sandbox_repository:$source_sha"
docker buildx build --platform linux/amd64 --provenance=false --push \
  --label "org.opencontainers.image.revision=$source_sha" \
  --metadata-file sandbox-build.json \
  -f "$release_context/validation-worker/Dockerfile.sandbox" \
  -t "$sandbox_tag" "$release_context"
rm -r -- "$release_context"
sandbox_digest="$(python -c 'import json; print(json.load(open("sandbox-build.json"))["containerimage.digest"])')"
sandbox_image="$sandbox_repository@$sandbox_digest"
docker run --rm --platform linux/amd64 --network none --read-only \
  --entrypoint cat "$sandbox_image" /runtime.json > sandbox-environment.json
python - "$sandbox_image" <<'PY' > round-runtime.json
import json, sys
with open("sandbox-environment.json") as stream:
    environment = json.load(stream)
print(json.dumps({"image": sys.argv[1], "environment": environment}, indent=2))
PY
```

Commit and push `round-runtime.json` as `rounds/{n}/runtime.json` to publish
that runtime's metadata. Export the published trusted image's filesystem into
`SANDBOX_ROOT` (or bake it into the Pod). The operator selects the pinned image and
prepares the filesystem; `/runtime.json` records its versions for inspection.
Do not copy a home directory, credentials or a submitted virtualenv into it.

## Worker image

Build the worker with the published sandbox image. The worker image contains
NsJail, the native evidence verifier and the sandbox filesystem. It replays the
saved preview renders from R2 and compares the generated JavaScript with the
canonical output.

```bash
docker buildx build --platform linux/amd64 --load \
  --build-arg SANDBOX_IMAGE=REGISTRY/validation-miner@sha256:DIGEST \
  -f validation-worker/Dockerfile.worker -t validation-worker:dev .
```

For a test competition branch, set `WITNESS_COMPETITION_BRANCH` to the branch that
owns the frozen R2 inputs. It defaults to `GITHUB_BRANCH`. Git checkpoints still
use `GITHUB_BRANCH`.

## Development

```bash
poetry run poe lint
poetry check --lock
poetry run python -m pytest -q
```

The Linux integration checks use a real sandbox and local HTTP GitHub/S3
fixtures. The resumption check interrupts validation after a committed prompt, fails a Git write
and an R2 read, resumes with the original input commit, checks saved PNGs and
rejections, probes synthetic credentials and worker network isolation, and checks
startup variables, NumPy imports, resource limits, clean scratch and unwritable
inputs across successive prompts. A failed final Git save retries the queued results. The retry check exhausts after a verified call,
checks persisted attempts and credits, and resumes without re-executing the prompt.
Its native proof-verifier subprocess is replaced with a fixture; the actual jail,
request/capture binding and budget logic run. Run
`poetry run python -m unittest discover -s tests -p test_validation.py -v` as root on Linux with NsJail
and `SANDBOX_ROOT` prepared. It skips when no sandbox runtime is configured.

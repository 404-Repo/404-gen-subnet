# Validation worker

Supports automatic validation jobs and [manual replays](MANUAL_REPLAY.md).
Manual replay runs with `poetry run python replay.py` and keeps its own local
reports. It uses a local Python subprocess with the operator's filesystem and
network access; the NsJail isolation below applies to automatic validation.

Automatic validation checks one candidate on a dedicated replay Pod. The trusted
worker reads GitHub and R2, verifies captured provider evidence, checks replayed JavaScript and
per-prompt budgets, and checkpoints progress in Git. Submitted imports and
`generate()` run in a fresh NsJail process for each prompt. The jail filesystem is
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
    "prompt_id": {"verdict": "passed", "reason": ""}
  }
}
```

The worker reads an existing checkpoint from the current competition branch and
continues using its original `ref`. Progress commits advance the branch without
changing the pinned submission, prompts, seed or canonical output references.
A new checkpoint records the supplied `--ref` before miner execution.

Each completed prompt is written through `GitBatcher` and flushed before starting
the next prompt. If a commit fails, pending progress stays in memory and its flush
is retried. A replacement worker reads the last committed checkpoint and skips
those prompts. An interruption during a prompt restarts that prompt.

Operational errors leave the prompt pending. The outer loop logs the error type,
waits `MIN_CHECK_STATE_INTERVAL` seconds and resumes. A completed validation failure
is checkpointed with its reason. `SIGTERM` and `SIGINT` stop between prompts or
interrupt the retry wait; cancellation terminates the active sandbox.

## Inputs and R2 reads

The worker reads the candidate's `submissions.json`, `prompts.txt`, `seed.json`,
`schedule.json` and `generated.json` at the checkpoint's pinned commit. It retrieves
the frozen round record and hash-checks each prompt image when that prompt starts.
Submitted source is fetched at its recorded commit and unpacked as regular files;
submitted links and unsafe archive paths are rejected. No submitted install hooks
are executed by the trusted worker.

R2 reads happen on demand:

- `R2_BUCKET`: frozen round metadata, current prompt bytes, exact call records and pricing.
- `R2_REPLAY_BUCKET`: capture files for a requested model call and saved preview PNGs or rejections for a render request.
- The current prompt's canonical JavaScript is fetched from its published URL.

There is no whole-candidate artifact download or evidence listing. Calls are
matched by their existing request identity. The trusted worker rechecks capture
bindings and runs the maintained native Witness TLS verifier before returning a
recorded response. Saved previews use request-keyed `renders/v2/`, with `v1/`
lookup using the original `RENDERER_ID`. Missing evidence or saved previews leave
the prompt pending; the worker never issues live provider inference.

The worker requires exact JavaScript correspondence and applies the shared
`PromptBudget` to the verified model calls consumed by that prompt. Temporary
source, jail and capture files are kept in private directories beneath `--output`
and removed when the job exits. Git holds the durable checkpoint.

## Miner isolation

NsJail 3.6 runs in standalone once mode with namespace creation disabled, using a
chroot, UID/GID 40000, no supplementary groups, no capabilities, `no_new_privs` and
the bundled seccomp allowlist. This profile works with RunPod's default container
capabilities. It requires a root worker able to chroot and change UID/GID; UID
40000 must be reserved for the miner in that Pod.

The worker copies only the pristine runtime, submitted source and credential-free
entry point into a job directory. Runtime and source files are owned by root and
unwritable by the miner. Before each prompt, the worker replaces the prompt image
and recreates `/scratch` for UID 40000. It clears the miner environment and closes
all inherited file descriptors except stdin/stdout/stderr. Worker storage,
expected outputs, raw evidence, tokens, home directories, SSH keys and agent
sockets stay outside the chroot. `/proc` and `/sys` are not mounted.

The seccomp policy denies networking, namespace and mount operations, tracing,
process-memory access and process-group/session changes. A private Unix socket
pair is allowed for asyncio. Every prompt starts a new process group; the worker
kills the group before reusing the jail or on cancellation. The process uses
normal CPU priority, a 2 GiB address-space limit, a 16 MiB per-file size limit,
128 file descriptors, 64 processes/threads and disabled core dumps.

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
SANDBOX_ROOT=/opt/validation-miner-root
SANDBOX_TIMEOUT=1800
MIN_CHECK_STATE_INTERVAL=120
LOG_LEVEL=DEBUG
```

GitHub access must allow reading source and writing the competition checkpoint.
R2 credentials need object reads from both buckets. They remain in the trusted
worker. `WITNESS_PROVIDER` selects the original `api` or `codex` request
normalization; its default is `codex`. `SANDBOX_TIMEOUT` is an operational watchdog
in seconds, not a competition time budget.

```bash
poetry run python main.py --round-num 7 --hotkey MINER_HOTKEY \
  --ref COMPETITION_COMMIT_SHA --output /private/validation/candidate
```

Build the pristine development miner runtime from the repository root with
`docker build -f validation-worker/Dockerfile.sandbox -t validation-miner:dev .`.
Export that trusted image's filesystem into `SANDBOX_ROOT`, or bake it into the
validation Pod image. It contains the local SDK sources and common Python
runtime dependencies. Freeze the runtime for the admitted miner; do not copy a
user home directory, credentials or a submitted virtualenv into it.

## Development

```bash
poetry run poe lint
poetry check --lock
poetry run python -m pytest -q
```

The single Linux integration check uses a real sandbox and local HTTP GitHub/S3
fixtures. It interrupts validation after a committed prompt, fails a Git write
and an R2 read, resumes with the original input commit, checks saved PNGs and
rejections, probes synthetic credentials and worker network isolation, and checks
clean scratch and unwritable inputs across successive prompts. Run
`poetry run python -m unittest discover -s tests -p test_validation.py -v` as root on Linux with NsJail
and `SANDBOX_ROOT` prepared. It skips when no sandbox runtime is configured.

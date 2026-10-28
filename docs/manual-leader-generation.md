# Manual leader generation

Run the incumbent's frozen Python application once for each published round.
The manual launcher uses the base miner's existing async runner and the current
witness Pod for inference, previews, final judging views, embeddings and saving.
It does not provision a generation service or change gateway configuration.

The initial source is `404-Repo/404-base-miner-gpt` at
`26c3ad32f9c4f202c9cd896c955695ab1964b75c`. The latest entry in the active
competition's `leader.json` selects that source. For subsequent rounds, the round
manager updates the incumbent's repository and commit; the same commands select
that latest entry. Supply a local clone of that repository as `--source`.
`--runner-source` remains a clone of the base miner containing the pinned commit.
No working-tree changes from either source checkout are executed.

## Before running

Use the Python 3.13 environment from the
[launch quickstart](../miner-reference/README.md#launch-quickstart), with
`404-witness-client==0.2.14` and `404-renderer-client==0.1.4`. Keep this complete
RC checkout: the launcher imports its shared result schema and view definitions.
The shared imports also require `tenacity`; install it in that environment:

```sh
python -m pip install 'tenacity==9.1.2'
python -m pip check
```

The operator needs a local sr25519 wallet hotkey registered on Finney netuid 17,
admitted by the current gateway. Its assigned round must be `gpt-0` for round 0.
The gateway operator must have frozen the official image-hash/ID mapping, seed,
allowed models, pricing and budgets for this round, with result saving enabled.
These are gateway prerequisites; this launcher does not override them or register
an unauthenticated `leader` identity. Pod allocation and replacement remain in
the authenticated SDK/gateway flow.

Prepare a private operator directory containing a file named `witness.json`:

```json
{
  "witness_url": "https://witness.subnet404.xyz",
  "proof_parameters": "/absolute/path/to/404-sdk/witness-server/verifier/params",
  "codex_home": "/absolute/path/to/private/leader-codex"
}
```

`proof_parameters` must contain the matching public `circuit.bin`, `proving.bin`
and `verifying.bin`. `codex_home` is optional; without it the SDK uses
`CODEX_HOME` or `~/.codex`. The config holds paths, not credential values.
Provision the operator's existing ChatGPT subscription through the current
Codex file-login flow on the execution machine:

```sh
CODEX_HOME=/absolute/path/to/private/leader-codex \
  codex -c 'cli_auth_credentials_store="file"' login
```

The SDK reads that directory's `auth.json` when preparing a model request.
When its access token expires within five minutes, it asks the configured
`codex` executable to refresh through `app-server account/read`, then rereads the
file. Codex owns refresh-token storage and rotation. This launcher supplies
`subscription="codex"` through the base runner and never uses an API-key fallback.
Keep the login directory writable and the Codex executable available throughout
the run. If login is revoked, refresh fails or subscription allowance is exhausted,
the finite runner attempts can finish with pending prompts. Restore the login or
allowance and rerun the same command; do not erase the witness's retained work.

Use one operator process per round/hotkey and one credential owner for the login
directory. The launcher locks its run directory and serializes SDK preparation
within that process, including credential loading/refresh. It does not coordinate
refresh with another process or promise indefinite unattended login.

The manual process executes inspected incumbent Python directly. It is a trusted
operator execution path, without sandbox isolation from the local wallet, login
or environment. Install the frozen source's required dependencies before a later
incumbent run. A synchronous/four-argument entry point fails this launcher's async
contract and requires a separately supported execution path.

## Freeze and launch round 0

Run these from an authenticated operator machine after round 0's `prompts.txt`
and `seed.json` have been published. The competition is
`404-Repo/404-active-competition`; local directory names are not its identity.
Use a fresh competition clone so another competition's local files cannot enter
the run. Set `LEADER_PY` to the prepared Python environment's executable.

```sh
LEADER_PY=/absolute/path/to/.venv/bin/python
LEADER_TOOL=/opt/404/404-subnet-rc/submission-collector/generate_leader.py
LEADER_STATE=/opt/404/404-leader-state
LEADER_RUN=/opt/404/leader-runs/gpt-0
LEADER_CONFIG=/opt/404/leader-operator/witness.json

git clone git@github.com:404-Repo/404-active-competition.git "$LEADER_STATE"
# For an existing dedicated clone, use: git -C "$LEADER_STATE" pull --ff-only

"$LEADER_PY" "$LEADER_TOOL" prepare \
  --competition "$LEADER_STATE" --round 0 \
  --source /opt/404/404-base-miner \
  --runner-source /opt/404/404-base-miner \
  --run-dir "$LEADER_RUN"

"$LEADER_PY" "$LEADER_TOOL" run \
  --run-dir "$LEADER_RUN" --witness-config "$LEADER_CONFIG" \
  --wallet-name miner --wallet-hotkey default \
  --parallel-prompts 8 --parallel-calls 12 --attempts 3
```

Use the actual local wallet name/hotkey if different from `miner/default`.
Wallet keys stay local. This authenticated hotkey may differ from the historical
hotkey in `leader.json`; both identities are retained in the handoff metadata.
`leader` is only the judge's bundle alias.

`prepare` resolves one competition Git commit, loads its official prompts and
seed, snapshots the incumbent's full source commit and the pinned base runner,
and downloads/hash-checks every image. It refuses missing/duplicate prompts,
non-HTTPS image URLs, source-origin mismatches or a missing Python entry point.
The frozen manifest is written only after preparation completes. Use a new
run directory if preparation fails before that manifest exists. Preparation does
not make model calls or write competition state.

`run` refuses changed snapshots, a changed authenticated hotkey/client environment,
a different witness origin, an assigned gateway round mismatch, or a Pod with a
different registered source commit. Final submission explicitly includes the
official seed so the Pod rejects a different frozen seed. It retains the gateway's
actual model-call identities, admission and accounting.

The runner logs each pending prompt and saved submission. Inspect
`$LEADER_RUN/progress.json` for saved/expected/failure counts and
`$LEADER_RUN/usage.json` for observed capture IDs, models and token usage.
Captures are deduplicated across retries. Unresolved calls remain explicitly
marked. This is local observed usage, not a complete settlement ledger or a
subscription/API invoice; missing observations do not mean zero remote usage.

## Resume and hand off

Repeat the identical `run` command after interruption, while the round's access
window and retained evidence remain available. Do not rerun `prepare`, edit the
snapshot, change the seed/models or delete the run directory. The base runner
reads `get_submitted()` before loading images and skips saved prompt IDs,
including explicit `None` outcomes. Pending generation reuses retained responses
for identical canonical model requests and request IDs. Preview rendering repeats;
its bytes must match for downstream requests to reuse recordings.

The Pod saves its canonical result index at:

```text
https://subnet404.xyz/rounds/gpt-0/<authenticated-hotkey>/submitted.json
```

Sources are under `submitted/<prompt-id>.js`; their corresponding folder holds
the 12 judging PNGs, `grid.png` and `embeddings.npz`. These remain under the real
hotkey. No artifacts or evidence are copied into an R2 `leader` namespace.

After generation, refresh the dedicated competition checkout and prepare the
judge's two local handoff files. This can run without a live Pod or login because
it reads the saved public index and artifacts:

```sh
git -C "$LEADER_STATE" pull --ff-only
"$LEADER_PY" "$LEADER_TOOL" handoff \
  --competition "$LEADER_STATE" --run-dir "$LEADER_RUN"

git -C "$LEADER_STATE" add -- rounds/0/leader/generated.json rounds/0/leader/source.json
git -C "$LEADER_STATE" diff --cached --stat
git -C "$LEADER_STATE" commit -m "Publish round 0 leader baseline"
git -C "$LEADER_STATE" push origin HEAD:main
```

`handoff` requires one terminal outcome for every official prompt and at least one
usable rendered result. It preserves explicit generation/render failures, rejects
empty or missing rows and foreign round/hotkey paths, and checks every referenced
JS file, judging view, grid and embedding object before writing any judge bundle.
It also checks that the competition's committed prompts, seed and incumbent still
match the frozen inputs. Different existing handoff files are never overwritten.

`rounds/0/leader/generated.json` uses the collector/judge's `GenerationResult`
schema (`js`, `views`, `failure_reason`, `size`), with absolute CDN URLs.
The judge obtains embeddings from `<views>/embeddings.npz`.
`source.json` records the frozen source and runner commits, competition snapshot,
published seed, historical leader identity, authenticated witness identity, and
submitted-index digest. It contains no credentials, raw model prompts or logs.
Keep the private run directory out of Git.

The collector still collects submitted candidate hotkeys. This manual Git handoff
provides the incumbent bundle separately. Deploy the judge readiness guard from
the same RC: when candidates exist it waits without scoring or finalizing until
every official prompt has a terminal leader result. A round with no candidates
can retain the incumbent without performing baseline generation.

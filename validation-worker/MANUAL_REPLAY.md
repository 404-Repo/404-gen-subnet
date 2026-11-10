# Local manual replay

An optional tool for manual re-checks and ad-hoc runs, independent of the regular
validation flow.

Replay one miner's frozen Python source for one prompt or an entire round. The
host resolves the repository/commit from the competition's `submissions.json`,
reads the official seed and retained prompt images, and fetches saved witness
traffic from R2. Saved responses are returned only for matching canonical
requests, providers, request IDs and subscription routing metadata. Responses are
loaded on demand without new model calls. An uncaptured request produces a
catchable SDK connection error, allowing the miner's retry or fallback logic to
run. Replay trusts the saved recordings.

The unchanged `main.py:generate` runs in a local Python subprocess using the same
interpreter and virtualenv as `replay.py`. Async `generate(image, seed, witness)`
and synchronous `generate(image, seed, witness, renderer)` are supported. The host
returns the original saved preview PNGs or structured rejections from R2. The worker's environment
excludes the host's credentials. Submitted code runs with your local user's
filesystem and network access.

## Setup

Run from `validation-worker/` using its Poetry environment:

```sh
poetry install
poetry run python replay.py --help
```

The worker project installs the SDK and storage dependencies used by the command.
An absolute script path also works from another directory in that environment.
Replay never installs packages, downloads models, creates cloud resources or sends
model requests through the replay client.

Set the following operator configuration in the host environment:

| Variable | Purpose |
| --- | --- |
| `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | Read/list access; create access on the replay bucket for v1 recovery |
| `R2_BUCKET` | Primary bucket for journals, frozen prompts and final artifacts; defaults to `404-gen` |
| `R2_REPLAY_BUCKET` | Required bucket for provider captures and saved preview renders |
| `WITNESS_DEPLOYMENT` | Deployment namespace that owns the saved calls |
| `WITNESS_COMPETITION_REPO` | State repository; defaults to `404-Repo/404-active-competition` |
| `WITNESS_COMPETITION_BRANCH` | State branch; defaults to `main` |
| `GITHUB_TOKEN` or `WITNESS_GITHUB_TOKEN` | Optional private-repository read access |
| `REPLAY_RENDERER_ID` or `WITNESS_IMAGE` | Optional additional legacy image to try before the built-in recent releases |

For the closest reproduction, use the approved Python/package versions and the
miner's permitted libraries. The current competition specifies Python 3.13/Linux
x86_64 and the packages in
[`runtime/requirements.txt`](../miner-reference/runtime/requirements.txt). The
local interpreter and package versions are saved in the replay log. Differences
in your local operating system or dependencies can affect output. The runner uses
this checkout's clients/runtime and does not execute source installation hooks.
Committed supporting regular files and executable bits are retained; unsupported
archive entries stop preparation and do not produce a competition verdict.

## Run

```sh
# Activate an existing virtualenv and load local configuration, if saved there.
source .venv/bin/activate
set -a
source .reports/replay.env
set +a

# One official prompt ID
poetry run python replay.py --hotkey HOTKEY --round test-0 --prompt PROMPT_ID

# All official prompts; stop on the first unsuccessful replay (default)
poetry run python replay.py --hotkey HOTKEY --round test-0 --all

# Keep checking subsequent prompts after a mismatch or unavailable prompt
poetry run python replay.py --hotkey HOTKEY --round test-0 --all --on-mismatch continue

# Run up to four prompts concurrently and check every prompt
poetry run python replay.py --hotkey HOTKEY --round test-0 --all --jobs 4 \
  --on-mismatch continue
```

`--jobs` sets the maximum number of concurrent prompt workers and defaults to
`1`. With the default `--on-mismatch stop`, the runner stops starting new prompts
after observing a failed result. Prompts already started finish and save their
results. `--on-mismatch continue` schedules every selected prompt. Results in
`summary.json` retain the original prompt order, with started, completed and
not-started counts. Automatic recovery runs within each prompt's worker slot;
stop/continue uses the final result after recovery.

Missing preview outcomes are tried automatically as connection failures, timeouts
or HTTP 429/500/502/503/504 errors. A failed execution that encountered a missing
render starts a fresh worker with another error sequence, using the same saved
model responses and renders. Sequences can differ between requests and between
retries of one request. The SDK's response handling preserves HTTP status and
exception causes so the miner runs its own retry/fallback code.

Recovery passes only when the model request checks, exact final output, saved
final grid and existing budget checks succeed. Saved PNGs and recorded rejections
are never replaced with assumed errors. Storage errors, corrupt evidence,
and missing final grids stop recovery. A budget violation rejects that trial;
remaining missing-render error sequences are still tried within the configured
limits.

`--recovery-attempts` limits fresh executions per prompt and defaults to `32`;
set it to `1` to disable automatic reruns. `--timeout` is the total time allowed
for a prompt, including recovery. Each execution is also limited to 16 missing
outcomes for one render request and 64 missing outcomes overall. Reaching a
limit finishes without a passing result.

Prompts that pass after a rerun have `recovered: true` and count separately in
`recovered_passed_prompts`. Their full error assignments and trial results are
saved in `render_recovery`; these are compatible assumed failures, not a claim
about the original HTTP statuses. Trial artifacts stay under
`replays/PROMPT/attempts/NNN/`; the usual output and render paths link to the
selected trial. All trials retain full logs; worker events include
`recovery_attempt`. Prompts that finish in one execution keep their existing layout.

`--hotkey` accepts a full hotkey or a unique, case-sensitive prefix such as
`5FRdf3HXns`. Prefixes are resolved against the selected round's frozen
submissions. The full resolved hotkey is logged and used for storage and cached
indexes. Missing or ambiguous prefixes stop preparation; ambiguous errors list
the matching full hotkeys.

`--round` accepts a round number or the competition's full round name. `--prompt`
and `--all` are mutually exclusive. No Witness image setting is required.
`--witness-image` (`--renderer-id` alias) optionally adds a legacy image to try
before the built-in release list; it defaults to `REPLAY_RENDERER_ID`, then
`WITNESS_IMAGE`.

The original provider route is inferred for each prompt from its first matching
saved request. Unrelated API/subscription records in the same round do not require
configuration. If both routes match the same first request, replay stops with an
ambiguity mismatch.

Preview outcomes use
`<storage-prefix>rounds/<round>/<hotkey>/renders/v2/` in `R2_REPLAY_BUCKET`.
The key hashes the exact raw HTTP path/query and request-body SHA256, matching the
request-only Witness writer format. Witness images are not part of v2 identity.
The exact saved PNG bytes are returned; a saved structured rejection raises
`RenderRejected` so the miner can follow its normal fallback logic. There is no
live renderer URL, API key or rendering cost.

On a v2 miss, the reader checks v1 keys for the known published Witness Pod images
from November 1–8, 2026. It lists the miner's render keys once per cold run and
computes candidate hashes locally. The first matching original PNG or rejection
is copied unchanged to its request-only v2 key in R2 and cached locally. Original
v1 objects are retained. Copies use create-if-absent writes; different existing
v2 bytes are not replaced. This recovery requires write access to
`R2_REPLAY_BUCKET`. Mixed Witness versions are checked per request, not per round.

Later replays read the v2 copy or local cache directly, without image history or
another legacy search. Cached requests do not list or read R2 again.

After a non-`None` output, replay checks the saved final grid in `R2_BUCKET` at
`<storage-prefix>rounds/<round>/<hotkey>/submitted/<prompt-id>/grid.png`.
The returned JavaScript must exactly match the saved source and the saved result's
`views` must identify that prompt's canonical submission directory. The final grid
is copied into the run's render logs. A preview without a saved PNG or rejection
raises `RenderUnavailable`, allowing the miner to retry or use its fallback.
This assumed render failure does not fail replay when the miner recovers and the
output, budget and saved final-grid checks pass. There is no live-render fallback.
Missing final grids, unreadable storage and corrupt saved records remain
missing-evidence results.

The pricing file defaults to `validation_worker/pricing.json`. Use `--pricing`
to select the table used for budget checks. Replays do not compare its hash with
capture metadata; the operator manages pricing updates. The round's retained
storage prefix is honored.
Only consumed response bodies are downloaded. Failed outcomes still retained in
the journals remain indexed. Usable response reuse counts once per prompt;
each prompt has its own budget. Unrelated exchanges need not be consumed.

Requests match exactly first. If that fails, inline PNG inputs may match by exact
decoded pixels, dimensions, pixel mode and PNG metadata. Compression and IDAT
chunk boundaries may differ. If no exact match exists, RGB and RGBA PNGs may
match with at most a 1/255 difference per RGB channel in at most 0.005% of each
image's pixels (one pixel per 20,000). Dimensions, mode, alpha and PNG metadata
remain exact. Frozen reference images keep exact pixels. All other request
fields, provider, request ID and session ID remain exact. Multiple qualifying
saved requests are ambiguous and stop that prompt. Final JavaScript output
matching remains exact.

PNG matching downloads recorded request bodies on demand for the same request ID
and session, verifies their recorded hashes and caches comparison digests in
`.reports/replay-indexes/ENDPOINT_BUCKET/png-requests-v1/`. Subsequent runs reuse
these digests. Requests needed for pixel comparisons are cached alongside them
and checked against their recorded hashes on every read. Original capture hashes
remain unchanged. `png_request_matched` reports exact decoded matches;
`image_tolerance_matched` reports tolerant matches with per-image changed-pixel
counts, total pixels and maximum channel differences. Both log the actual and
recorded request keys, and `model_request` retains the actual request body.
Accounting uses the recorded call ID.

Each prompt summary separates `request_matches` into `exact`,
`exact_png_pixels` and `image_tolerance`, counting distinct actual requests. Its
`image_tolerance_matches` list retains the tolerant match details. The run summary
reports `exact_passed_prompts` and `tolerant_passed_prompts` separately. A tolerant
pass means replay matched within the documented image limits.

The request index is reused automatically for the same R2 endpoint, bucket,
deployment, round, hotkey, storage prefix and pricing file. A cache hit logs
`request_index_reused` with the saved index path and capture count, and copies the
index and journal snapshots into the new run without listing or downloading R2
call journals. Add `--refresh-index` to any replay command to fetch journal
updates. Until refreshed, the saved snapshot is used even if new calls arrive.

R2 GET and LIST API calls use Boto3's standard retry policy with up to five total
attempts, including the initial request, and exponential backoff. Retryable
request timeouts, connection errors, throttling and transient server errors retry
the affected request while keeping the current preparation progress and caches.
If retries are exhausted, the error is reported normally.

A timeout while downloading an object's body retries the complete object read,
up to five reads with delays of 1, 2, 4 and 8 seconds. These read attempts are in
addition to the SDK's request retries. Each failed response is closed and its
partial body discarded. `r2_read_retry` logs the object key, attempt and delay.

Failed captures without a response manifest use the immutable `failure.json`
record. Older captures may use a terminal `status.json` when `failure.json` is
absent. The saved request digest and failure type must match the call journal.

Index cache entries in the checkout's `.reports/replay-indexes/` directory
reference `bundle/` snapshots in saved runs. Keep those snapshots to retain the
cached index; a missing snapshot is fetched again.

Consumed response evidence is cached automatically under
`.reports/replay-indexes/ENDPOINT_BUCKET/responses-v1/`. Each entry binds the
deployment, round, hotkey, storage prefix, pricing hash and recorded call,
including its manifest hash. Original manifest, request, response and saved
failure files are copied into each new run and validated before use. A changed
call or manifest uses a separate entry; invalid cached evidence fails the prompt.
Evidence already downloaded into the indexed run is imported on demand. The
response cache remains usable if that old run is removed. `--refresh-index`
fetches journal updates while reusing matching response evidence. Every replay
executes the miner, reuses saved model/render outcomes and writes full logs in a new output directory.

Preview PNGs and rejections are cached under
`.reports/replay-indexes/ENDPOINT_REPLAY_BUCKET/renders-v1/`; final grids use
`.reports/replay-indexes/ENDPOINT_PRIMARY_BUCKET/final-renders-v1/`. Identical
requests reuse the saved outcome without another R2 read. Provider response and
PNG-request caches also use the replay bucket's identity. Journal indexes use the
primary bucket. `--refresh-index` refreshes call journals while retaining matching
cached bodies and renders.

## Logs and results

Each run creates a private `.reports/replays/TIMESTAMP/` directory; `--output`
selects another new directory. Existing output directories are never overwritten.
The console shows prompt progress, model request/response summaries, render
results, budget exclusions and errors. Index progress appears every 100 captures.
Image data URLs, provider SSE chunks and generated source are omitted. Raw miner
stdout and stderr appear only in saved logs, including JS printed line by line.
`console.log` keeps the full diagnostic text, including Python/native stdout and
stderr, requests, recorded SSE responses, stream
operations, source, output differences and exception tracebacks. Configured
credential values are redacted. Raw prompts, responses and source stay in the
private output.

`events.jsonl` contains structured events. `manifest.json` records selected source
and inputs. `saved_render_loaded` records the original object key, whether a local
cache was used, byte count and content hash. `render_request`, `render_response`
and `render_rejected` retain the miner's render inputs and saved outcomes.
`final_render_load_started` identifies the saved submission-grid check.
`saved_render_recovered` records the legacy image, original object key and new v2
key after a successful copy.
`assumed_render_failure` records previews without a saved outcome. Their render
indices appear in each prompt's `assumed_render_failures`; the matching
`renders/INDEX.failure.json` marks the error as assumed.

`bundle/` keeps journal snapshots, saved request/response bytes and
capture metadata. `replays/PROMPT_ID/` contains requested render source/options,
PNGs/rejections, returned output, exact saved
and reproduced JavaScript, an output diff when needed and `summary.json`. The
top-level summary retains results when stopping early or receiving Ctrl-C.

Exit status is zero only when every selected prompt completes all available
checks successfully. A mismatch, unreadable saved evidence, rejected output,
an unhandled render failure, missing final grid or watchdog expiration returns
nonzero. `continue` advances to the next prompt; it does not supply a guessed
response to a divergent prompt.
`--timeout` defaults to 600 seconds per prompt. Ctrl-C stops all active worker process groups;
each prompt's watchdog stops its own worker group. Host R2 operations have their own bounded timeouts.

The independently saved R2 submission is the expected result. Explicit saved
`generation_failed` is compared with `None`; an absent submission is not `None`.
JavaScript is compared byte-for-byte, including line endings. A successful local
summary means the returned model responses matched their requests, final output
reproduced, recorded usage stayed within budget and the required saved renders
were available. `historical_renders_available` reports whether saved render outcomes
were loaded; `final_render_available` is true after checking the saved grid for a
non-`None` output.
`assumed_connection_failures` lists requests with no captured outcome;
`unknown_usage_calls` lists calls excluded from both the call and credit budget
because no token usage was recorded. These conditions do not fail replay.

The saved preview cache contains successful PNGs and structured rejections.
Older runs may have no preview cache, and expired evidence is unavailable.
Original Python observations and exact historical transport failures are not retained.
This command records a new diagnostic trace and reuses saved responses, including
provider errors. A failed capture with a bound saved request and failure status
reproduces the SDK capture error so the miner can run its retry or fallback logic.
Repeated attempts without a uniquely recorded outcome, original delivery errors
and causal scheduling cannot be reconstructed. A cached index miss checks the
requested R2 journal directly before assuming a model connection failure.
Unreadable or inconsistent known evidence still stops that prompt. SDK validation
errors remain catchable. Output comparison and saved final-grid verification
finish before accounting validation.
Results are local diagnostics, not terminal candidate validation; the command
writes recovered preview outcomes to `renders/v2/` in the replay bucket. It does
not write competition verdicts, call journals, final submissions or Git.

## Offline checks

The tests use saved JSON/SSE/PNG/rejection fixtures, mocked Git/R2 services and real local
Python subprocesses. The script startup test runs from another directory with
isolated Python imports. These checks establish replay correspondence, not the
recordings' authenticity.

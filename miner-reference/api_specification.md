# Batch Generation API Specification

> **Note:** This document defines the HTTP contract between the orchestrator and miner pods. It is the companion to the **Three.js Output Specification** (what miners produce) and the **Runtime Specification** (how submissions are validated and rendered). All three documents are required reading.

## Overview

The orchestrator launches your Docker image on a GPU pod, sends prompts in sequential batches, and collects JavaScript source files as output. You own scheduling, parallelism, GPU allocation, failure recovery, and hardware diagnostics. The orchestrator only cares about the final output.

### Why this design

The previous per-prompt HTTP model had the orchestrator managing retries, pod health, and degraded-GPU detection centrally. This was expensive and not transparent — miners had no control over scheduling, parallelism, or recovery from flaky inference.

The batch API shifts that control to miners. The orchestrator's job is reduced to delivering prompts and collecting results. Miners own the execution loop: how to distribute work across GPUs, when to retry a failed generation internally, how to diagnose hardware problems, and whether to spend a pod replacement or push through on a degraded node. Smart scheduling and diagnostics are now competitive advantages, not hidden orchestrator logic.

The pod replacement budget (4 pods total — your initial pod plus 3 replacements) makes this concrete. Your code receives `replacements_remaining` on every `/status` poll and decides whether a detected problem is worth burning a replacement for. A miner with better diagnostics wastes fewer pods, completes more batches, and scores higher.

Your service must expose four HTTP endpoints on port **10006**:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness probe during pod startup |
| `GET` | `/status` | State reporting, polled continuously |
| `POST` | `/generate` | Accept a batch of prompts |
| `GET` | `/results` | Return completed generation output |

## Hardware

Each pod has **4× H200 SXM** GPUs (141 GB HBM3e each, ~564 GB total VRAM). How you distribute work across GPUs is entirely up to you.

## Pod Lifecycle

### State Machine

Your service transitions through these states:

```
              ┌──────────────┐
  pod start → │  warming_up  │
              └──────┬───────┘
                     │ models loaded
                     ▼
              ┌──────────────┐
              │    ready     │ (initial state only — first batch)
              └──────┬───────┘
                     │ POST /generate
                     ▼
              ┌──────────────┐
              │  generating  │
              └──────┬───────┘
                     │ all prompts processed
                     ▼
              ┌──────────────┐
              │   complete   │ ◄─── stays here until next POST /generate
              └──────┬───────┘
                     │ POST /generate (next batch)
                     ▼
              ┌──────────────┐
              │  generating  │  ...
              └──────────────┘
```

The `ready` state is only used once — after warmup, before the first batch. After that, the service cycles between `generating` and `complete`. The orchestrator drives the transition out of `complete` by sending the next batch via `POST /generate`.

At any point where `/status` is polled, the service may also return `replace` to request a new pod (see Pod Replacement).

### Sequence

1. The orchestrator deploys your Docker image on a 4×H200 pod.
2. It polls `GET /health` until it returns `200`, confirming the service is up.
3. It polls `GET /status`. Your service reports `warming_up` while loading models.
4. Once `/status` returns `ready`, the orchestrator sends the first batch via `POST /generate`.
5. It polls `GET /status`. Your service reports `generating` with a progress count.
6. Once `/status` returns `complete`, the orchestrator calls `GET /results` to collect output.
7. The orchestrator sends the next batch via `POST /generate`. Your service transitions from `complete` directly to `generating`.
8. Repeat until all batches are processed.

## Batch Structure

A round consists of **128 prompts** split into **4 sequential batches of 32**. Batches are sent one at a time — the orchestrator waits for the current batch to complete before sending the next.

Models stay loaded between batches. There is no container teardown between batches within the same pod.

## Endpoints

### `GET /health`

Liveness probe. The orchestrator polls this during pod startup before switching to `/status` polling.

**Request:**

No query parameters. An `Authorization` header may be present (see Authentication).

**Response:**

Return `200` with any body when the HTTP server is accepting connections. The response body is ignored.

Return any non-`200` status (or refuse the connection) while the server is still starting up.

### `GET /status`

Reports current pod state. The orchestrator polls this continuously after the pod is healthy.

**Request:**

| Query Parameter | Type | Description |
|-----------------|------|-------------|
| `replacements_remaining` | `int` | Number of pod replacements still available in the budget. `0` means no replacements left. |

**Response:**

```json
{
  "status": "warming_up" | "ready" | "generating" | "complete" | "replace",
  "progress": null | <int>,
  "total": null | <int>,
  "payload": null | <object>
}
```

| Field | Type | Present When | Description |
|-------|------|-------------|-------------|
| `status` | `string` | Always | One of the five status values. |
| `progress` | `int \| null` | `generating` | Number of prompts processed so far. |
| `total` | `int \| null` | `generating` | Total prompts in the current batch. |
| `payload` | `object \| null` | Any state | Optional diagnostic metadata. The orchestrator logs it but does not act on its contents (except for `replace`, where it is recorded as the replacement reason). |

**Status values:**

| Status | Meaning | What the orchestrator does |
|--------|---------|---------------------------|
| `warming_up` | Models are loading. | Keep polling. |
| `ready` | Ready to accept the first batch. | Send first batch via `POST /generate`. |
| `generating` | Batch is being processed. | Keep polling. Track `progress`/`total`. |
| `complete` | Batch results are ready for download. | Call `GET /results`, then send next batch via `POST /generate`. |
| `replace` | Pod hardware is degraded; request a new pod. | Discard current batch, tear down pod, deploy a new one (costs one replacement). |

**`payload` examples:**

```json
{
  "status": "warming_up",
  "payload": { "stage": "loading_unet", "progress": 60, "total": 100 }
}
```

```json
{
  "status": "replace",
  "payload": {
    "benchmark": "gpu_health",
    "threshold_tflops": 30.0,
    "threshold_vram_gb": 134.0,
    "all_passed": false,
    "gpus": [
      { "gpu_id": 0, "tflops": 55.2, "vram_gb": 141.0, "passed": true },
      { "gpu_id": 1, "tflops": 12.1, "vram_gb": 141.0, "passed": false }
    ]
  }
}
```

**`replace` behavior.** Your service may return `replace` during any status poll when it detects hardware degradation (insufficient VRAM, failing GPU, thermal throttling, etc.). This is a cooperative signal — the orchestrator honors it and deploys a new pod.

> **Warning:** Returning `replace` when `replacements_remaining` is `0` ends the round. The orchestrator interprets it as a crash and stops sending batches. Always check `replacements_remaining` before returning `replace` — if it's zero, push through on the current pod even if hardware is degraded. A suboptimal pod that completes batches scores better than a round that ends early.

### `POST /generate`

Submit a batch of prompts for generation. The orchestrator calls this after `/status` returns `ready` (first batch) or `complete` (subsequent batches, after collecting results via `GET /results`). Accepts from both `ready` and `complete` states.

**Request:**

```json
{
  "prompts": [
    {
      "stem": "a1b2c3d4",
      "image_url": "https://storage.example.com/prompts/a1b2c3d4.jpg"
    }
  ],
  "seed": 42
}
```

| Field | Type | Description |
|-------|------|-------------|
| `prompts` | `array` | List of prompt items for this batch. |
| `prompts[].stem` | `string` | The prompt image filename without its extension (e.g., `a1b2c3d4` from `a1b2c3d4.jpg`). Lowercase alphanumeric, unique within a round. Use as the filename in the results ZIP (`{stem}.js`) and as the key in `_failed.json`. |
| `prompts[].image_url` | `string` | URL to the prompt image. Your service downloads this. |
| `seed` | `int` | Seed for the round. Use for any deterministic randomness in your pipeline. |

**Response:**

Return `200` to acknowledge the batch. Generation runs asynchronously — the orchestrator polls `/status` for progress.

```json
{
  "accepted": 32
}
```

| Field | Type | Description |
|-------|------|-------------|
| `accepted` | `int` | Number of prompts accepted for processing. |

**Idempotency.** `POST /generate` is idempotent. If the orchestrator retries with the same set of prompt stems and the service is already `generating` or `complete` for that batch, return `200` with the same `accepted` count. Do not restart generation. The orchestrator relies on this to safely retry when the network drops a response.

Idempotency is determined by matching the set of stems in the request against the batch currently being processed. A request with a different set of stems while already generating is not a retry — return `409`.

**Error responses:**

Return `409` when the service cannot accept the batch. The response body **must** include the current state:

```json
{
  "detail": "Cannot accept batch",
  "current_status": "generating"
}
```

| `current_status` | Orchestrator action |
|-------------------|---------------------|
| `generating` | Different batch in progress — this is unexpected, not a retry. |
| `warming_up` | Pod not ready yet — wait and retry later. |
| `replace` | Pod requesting replacement — proceed with replacement flow. |

Note: `complete` is **not** a valid 409 reason. The service accepts new batches from `complete` (this is the normal subsequent-batch path) and accepts retries with matching stems via the idempotency rule above. There is no API signal that distinguishes "complete with results uncollected" from "complete with results collected" — the orchestrator is responsible for calling `GET /results` before sending the next `POST /generate`.

### `GET /results`

Download the completed batch output as a streamed ZIP archive. The orchestrator calls this after `/status` returns `complete`.

**Response:**

| Header | Value |
|--------|-------|
| `Content-Type` | `application/zip` |
| `Transfer-Encoding` | `chunked` |

The response **must** use chunked transfer encoding. Do **not** set a `Content-Length` header. GPU provider reverse proxies and load balancers may enforce response size limits or idle timeouts on buffered responses — chunked streaming keeps data flowing and avoids premature connection drops.

The response body is a ZIP archive containing one file per successfully generated prompt. Each file is named `{stem}.js` and contains a complete ES module conforming to the Output Specification.

Archive structure for a batch of 32 prompts (30 succeeded, 2 failed):

```
a1b2c3d4.js
e5f6g7h8.js
... (28 more .js files)
_failed.json        (optional, present only if any prompts failed)
```

Each `.js` file is a UTF-8 JavaScript module with a single `export default function generate(THREE)` returning a Three.js scene root, subject to all constraints in the Output Specification.

**`_failed.json`** is an optional manifest included when any prompts could not be generated:

```json
{
  "i9j0k1l2": "inference timeout",
  "m3n4o5p6": "image download failed"
}
```

The keys are stems; the values are short failure reason strings. The orchestrator logs these but does not penalize the miner beyond scoring the failed prompts as zero. If every prompt succeeded, omit `_failed.json` entirely.

**Partial results are expected.** If you cannot generate one prompt in a batch, skip it and return the rest. 31 out of 32 successful `.js` files is better than timing out and losing the entire batch.

**Retryability.** `GET /results` is safe to call multiple times. Your service stays in `complete` state until the orchestrator sends the next batch via `POST /generate`. If a download fails (connection drop, timeout), the orchestrator calls `/results` again. Your service must return the same ZIP archive on every call while in `complete` state.

**Edge cases:**

- Each prompt stem must appear in exactly one place: either as a `{stem}.js` entry or as a key in `_failed.json`. If a stem appears in both, or in neither, the orchestrator marks it as failed.
- An all-failed batch is valid: return a ZIP containing only `_failed.json`. An empty ZIP (zero entries, no `_failed.json`) is treated as a complete batch failure.
- Use `ZIP_DEFLATED` compression. Typical JS source files compress ~50%.
- Maximum ZIP size is approximately **32 MB** (32 prompts × 1 MB max per file, before compression). The orchestrator sets stream read limits accordingly.

**Error responses:**

| Status | Condition |
|--------|-----------|
| `409` | Not in `complete` state. |

## Authentication

When the pod is deployed on a GPU provider that requires authentication, the orchestrator includes an `Authorization` header on all requests:

```
Authorization: Bearer <token>
```

Your service should accept but not require this header. Pass it through if your service makes calls to provider-specific APIs. Do not reject requests that lack the header.

## Pod Replacement Budget

Each miner starts with a budget of **4 pods per round**: the initial pod plus **3 replacements**. Every pod that gets used — whether by your code requesting `replace`, a health check timeout, or a crash — costs one pod.

| Event | Cost | Batch impact |
|-------|------|-------------|
| Your code returns `replace` | 1 pod | Current batch discarded, restarted on new pod. |
| `/status` unresponsive after retries | 1 pod | Current batch discarded, restarted on new pod. |
| Pod crashes | 1 pod | Current batch discarded, restarted on new pod. |
| All 4 pods spent | Round ends | Scored on whatever batches completed. |

When the orchestrator detects an unresponsive pod (no valid `/status` response after multiple retries), it replaces the pod. This accounts for transient network issues — a few failed polls are retried before declaring the pod dead.

**Batch atomicity.** A pod replacement mid-batch discards the entire batch. Partial progress from the failed pod is not carried over. The batch is re-sent from scratch on the new pod. Completed batches from previous pods are preserved.

## Time Budget

Your round has two hard deadlines:

| Phase | Time Limit | What happens |
|-------|-----------|--------------|
| **Warmup** | **4 hours** | From pod deploy to `/status` returning `ready`. If your service is not ready within 4 hours, the pod is terminated and a replacement is deployed (costs one pod). |
| **Generation** | **2 hours** | From the first batch submission to the last `/results` download. All 128 prompts (4 batches of 32) must be generated within this window. |

Batches submitted after the 2-hour generation deadline are **not accepted** — the orchestrator stops sending new batches and scores only what was completed in time.

The **warmup timer restarts** with each new pod. If your first pod crashes after 30 minutes of warmup, the replacement pod gets a fresh 4-hour warmup window. However, the **generation timer does not restart** — time lost to a mid-batch pod replacement still counts against the same 2-hour generation window. Budget your replacements accordingly.

| Timer | Value | Notes |
|-------|-------|-------|
| Warmup deadline | 4 hours | Per pod — restarts on each replacement. |
| Generation deadline | 2 hours | Wall clock — does not restart on replacement. |
| Status poll interval | ~10 seconds | Approximate; the orchestrator may adjust. |
| Status unresponsive threshold | Multiple consecutive failures | Transient failures are retried before the pod is declared dead. |

Design your solution to be responsive to `/status` polls regardless of generation load — run generation in a background task, not in the request handler.

## Scoring

Scoring is based on **output quality**. Speed within the time budget does not matter — finishing in 30 minutes scores the same as finishing in 119 minutes. What matters is the quality of the Three.js scenes you produce.

Each JavaScript module is validated and rendered per the Output and Runtime Specifications. The rendered image is then compared against the original prompt image by a **VLM (Vision-Language Model) judge** — the same evaluation pipeline used in previous 404 competitions. The judge evaluates how well your rendered output matches the prompt image in terms of geometric correctness and visual fidelity.

**Any failed prompt is an automatic loss.** A prompt fails if:

- Your service does not return a `.js` file for that stem (missing from the ZIP).
- The returned module fails static analysis, execution, or post-execution validation (see Output Specification § Failure Semantics).
- The render run fails.

There is no partial credit. A failed prompt loses to every other miner's output for that prompt, regardless of how good your other outputs are. Maximize reliability first, then optimize quality.

## Implementation Notes

- **Run generation asynchronously.** `POST /generate` should return immediately after accepting the batch. Do the actual work in a background task and report progress through `/status`.
- **Handle `/status` under load.** The orchestrator polls `/status` every ~10 seconds during generation. If your `/status` handler blocks on generation work, the orchestrator may think the pod is dead.
- **Download prompt images yourself.** The `image_url` in each prompt points to a storage URL. Your service is responsible for downloading these images. Failed downloads should be treated as failed prompts (return in `failed`), not as a reason to crash the batch.
- **Guard against crashes.** If your generation task crashes, transition to `complete` with whatever partial results you have. A pod stuck in `generating` forever will be detected as unresponsive and replaced, costing a pod from your budget.
- **Test locally before deploying.** A buggy service that crashes on startup burns through pods fast. Each crash costs one pod from your budget with nothing to show for it.

## See Also

- **Three.js Output Specification** — what miners must produce (JavaScript module format, constraints, allowed APIs).
- **Runtime Specification** — how submissions are validated and rendered (sandbox, static analysis, rendering pipeline).

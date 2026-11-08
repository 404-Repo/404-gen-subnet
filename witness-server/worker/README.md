# Witness worker

The worker runs on an ordinary CPU Pod with a direct authenticated TLS service on
`8000/tcp`. The gateway supplies the miner with its public mapped endpoint,
certificate authority. The Pod authenticates its assigned hotkey. Outbound gateway connections
carry lifecycle control. The Pod owns per-call state and relays the miner's TLS connection to OpenAI. The verifier checks the
handshake, credential proof, request and response.

The worker saves authorization and the request before forwarding billable bytes.
After release, it captures and saves the response even if the miner or gateway
connection disappears. The saved response precedes delivery to the miner. A local
spool and R2 records support persistence retries without another provider request.

Once the relay starts, the miner has 60 seconds to send its ClientHello and five
minutes total to authorize the request. These fixed deadlines include transfers
and proof preparation; sending occasional bytes does not extend them. The
exchange deadline bounds capture and the configured generation phase.

Evidence lives under `rounds/<round>/<hotkey>/<exchange-id>/`. Generation
recordings retain invocation and delivery order. Durable claims and usage live in
R2; Pod identity, ownership and lifecycle come from RunPod.

The Pod saves capture, verifier and reservation failure facts atomically in the
exchange's private spool under `diagnostics/<record-id>.json`. Each record is
immutable. The ID identifies its operation, exchange identity and failure facts;
repeated observations reuse the original bytes and observation time. Recovery
failures have separate records from the original capture failure. The Pod uploads
these bytes to the same path in the exchange's R2 directory before spool cleanup.
The configured storage prefix applies. Upload retries and Pod restarts retain
pending records without repeating a provider request.

The record includes the observed stage and reason, exception and cause types,
bounded Python stack locations, verifier mode/exit status and safe Node stack
locations when available. Call, exchange and Pod identities correlate the evidence;
Python version, the Pod specification fingerprint and proof/pricing pins identify
the runtime. Reservation reasons distinguish unsupported models/tiers from
unavailable or busy ledgers. These records do not require `WITNESS_TIMINGS=1` and
contain no exception messages, locals, source lines, raw subprocess output or
credentials. They remain private and are excluded from downloadable evidence.

`failure.json` is the small outcome marker with `status` and `error_type`.
Call state and the manifest determine the outcome; diagnostics do not assign blame
or decide whether a call can be retried. Diagnostic collection errors emit a
content-free `witness_diagnostic_error` event and do not interrupt finalization.
Diagnostic uploads follow live response delivery; pending uploads keep the spool
until R2 accepts the saved bytes.

Content-addressed completed evidence lives under `evidence/v1/`. The Pod returns
expected hashes and public evidence URLs through authenticated TLS;
the miner downloads saved bytes directly from R2. Private journals, pending
spools, allocation capabilities and TLS keys remain private. The direct listener
verifies signed requests from the assigned hotkey and checks the durable R2
selection fence before capturing and authorizing new calls. It ignores bearer
authorization and does not recheck miner registration. `WITNESS_ROUND_END` is
the fixed generation deadline passed at allocation; an empty value means the
round explicitly has no deadline. Generation, rendering and saved-result
operations do not require a running gateway or renewed permission.

## Rendering and artifact storage

`R2_BUCKET` holds final results, frozen prompts and operational state.
Required `R2_REPLAY_BUCKET` holds provider evidence, preview renders and replay
artifacts; `R2_REPLAY_CDN_URL` is its public HTTPS origin. Replay artifacts expire
after 10 days; final-result artifacts after 60 days.

The Pod caches preview PNGs and structured rejections by exact request and worker
image. Outcomes are uploaded before responding and reused for identical requests.

Each miner gets 10,000 new successful previews per round. The Pod counts saved
PNGs at startup and keeps the count in memory. Cache hits, rejections, failed
renders and failed uploads do not count. New misses return HTTP 429 when exhausted;
cached outcomes remain available. Final judging renders and validation reads do
not consume this allowance.

Preview rendering has a 30-second browser deadline per request. Final saving
renders eight white views in two batches of four, four gray views and a four-view
grid; each request has a 150-second browser deadline. The deadline starts after
acquiring a render slot and includes browser setup, module execution, rendering,
PNG encoding and context cleanup. Queue waiting, validation, uploads and embeddings
are separate. Puppeteer's protocol timeout is 180 seconds.

Worker HTTP reads wait for queued renderer work; the browser deadline bounds
each admitted render. Final submission clients must also allow queue waiting and
all four requests: witness client 0.2.19 and renderer client 0.1.5 do this.
The older 0.2.17 / 0.1.4 bundle has a 150-second final-save read timeout.
The public preview proxy uses only the preview budget.

Paths under `<storage-prefix>rounds/<full-round-name>/<hotkey>/`:

- `renders/v1/`: cached PNGs and rejections, named by request hash.
- `submitted/<prompt-id>.js` and `submitted/<prompt-id>/`: final source, judging
  views, grid and embeddings.
- `submitted.json`: result index read by the [collector](../../submission-collector/README.md).

Final saves are immutable; retrying the same output returns the saved result.
`None` records `generation_failed`; renderer rejection records `render_rejected`
(HTTP 422). Storage, render and embedding failures are retryable. One final save
runs at a time, with 256 queued; overflow returns HTTP 503 `save_queue_full`.
Saved results remain readable after admission closes.

A browser deadline returns HTTP 504 `render_timeout` and leaves the prompt
pending. The JavaScript source can already exist in storage; a completed save
requires every judging view, grid, embeddings and the result index.

## Reading order

1. [pod.py](witness_pod/pod.py): worker activation, capture ownership, persistence retries and drain.
2. [identity.py](witness_pod/identity.py): canonical miner identity and round-scoped paths.
3. [channel.py](witness_pod/channel.py): bounded TLS and proof messages over the WebSocket.
4. [worker.py](witness_pod/worker.py): verification, provider capture and evidence persistence.
5. [storage.py](witness_pod/storage.py): R2 evidence access.
6. [verify.mjs](witness_pod/verify.mjs): entrypoint into the TLS/ZK verifier.

## Build and deployment

Build from the repository root:

```sh
docker build --platform linux/amd64 -f witness-server/worker/Dockerfile.pods -t 404-witness-pods:local .
```

The image includes the worker, Node, the native verifier, `verifying.bin`,
Chromium, the renderer and the Python dependencies for embeddings and result
saving. It starts with `python -m witness_pod`. Set `WITNESS_IMAGE` to this image's
immutable digest. Build target
`gateway` from the same Dockerfile for gateways and the independent revivor;
set `WITNESS_GATEWAY_IMAGE` to that image's digest.
Use the [Pod guide](../../witness-gateway/PODS.md) for operator settings,
capabilities, TLS, evidence storage and resource limits. Reproduce a tested
runtime using its full manifest, including any required bootstrap.

New Pods start dormant. The gateway selects the exact Pod ID before it can accept
inference work. Both provider routes share the deployment: API requests use
`api.openai.com/v1/responses`, while subscription requests use
`chatgpt.com/backend-api/codex/responses`. The verifier checks the selected route.

From this directory, check the built image and Docker context filtering:

```sh
WITNESS_TEST_IMAGE=404-witness-pods:local python -m pytest -q tests/test_container.py
```

See [server checks](../README.md#local-checks) for setup. Local TLS tests use
WebSockets and native credential proofs with local provider fixtures.

For local use, install with `python -m pip install --no-deps -e .` after the
runtime dependencies. Offline replay export uses `python -m witness_pod export`;
see the [replay guide](../../witness-gateway/PODS.md#verified-offline-replay).

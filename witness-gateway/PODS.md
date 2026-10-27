# Python witness gateway and ordinary CPU Pods

The Python gateway authenticates miners, allocates one current witness Pod per
miner, and returns its direct TLS endpoint. Witness Pods own call reservation,
authorization, recovery and provider evidence retention. They perform verification and
provider capture; they never execute miner programs. An independent revivor
restores the gateway and cleans up exactly owned Pods. RunPod owns Pod identity,
lifecycle and capacity. Private R2 objects retain model-call records, usage and
evidence. SQL is not required.

Imports, installation, tests and `--help` do not create infrastructure.

## HTTP API

Witness API routes use the `/v1` prefix. Configure `witness_url` and
`WITNESS_ORIGIN` with the gateway's HTTPS origin; clients and workers append the
versioned paths. Signatures cover the complete path, including `/v1` and any
renderer query string.

| Service | Method | Path |
| --- | --- | --- |
| Gateway | GET | `/v1/pods/current` |
| Gateway | POST | `/v1/pods/start` |
| Gateway | GET | `/v1/pods/{start_id}` |
| Gateway | GET (WebSocket) | `/v1/control/{hotkey}/{start_id}` |
| Assigned Pod | POST | `/v1/calls` |
| Assigned Pod | GET | `/v1/calls/{exchange_id}` |
| Assigned Pod | GET (WebSocket) | `/v1/exchanges/{exchange_id}/connect` |
| Assigned Pod | POST | `/v1/render/grid` |
| Assigned Pod | GET | `/v1/artifacts/context` |
| Assigned Pod | POST | `/v1/submissions` |
| Assigned Pod | GET | `/v1/submissions/{prompt}` |

The gateway's unauthenticated health check is `GET /health`. The Pod forwards
preview rendering to the internal renderer's `POST /render/grid` endpoint.

## Execution and replay contract

The miner supplies `generate(image, seed, witness, renderer)`. Model calls need no
prompt binding, recording handle or finish operation. The execution host invokes
the function; its witness boundary automatically observes calls. The validator
uses that execution's observations and authenticated capture evidence to run the
same submitted code with the expected image and seed.

### Supplied inputs and prerequisites

The host must freeze the source archive, dependencies, relevant environment,
image bytes and any content the program reads. An image URL alone is insufficient:
freeze and verify its content before execution. The generation seed is an input
to the program's local algorithm, separate from the model-call `request_id`.
Neither is automatically sent to OpenAI as a randomness seed.

The renderer must reproduce its bytes, structured rejections and relevant errors
under the same build/options. Time, external files, process state, random sources,
thread scheduling and alternate network access must be controlled or excluded by
the execution host. Witness metadata and TLS proofs do not make arbitrary Python
code deterministic or attest that it worked on a particular prompt.

`miner_runtime.runner.run_generation` and `miner_runtime.validator.run_replay`
are development execution-host helpers, not a sandbox. The submitted function sees
only the supplied clients. An actual trusted host must keep evidence, expected outputs, observation state and the replay engine
outside miner access, including imports, native code and child processes. These
helpers observe the witness boundary only; they do not implement that isolation,
a shared renderer/model scheduler, or the competition's complete validation job.

### Model-call interface and identity

`witness.responses.create(model=..., input=...)` returns an OpenAI `Response`.
Failed/incomplete Responses retain their status, usage and output. HTTP errors
raise the corresponding OpenAI SDK status-error class; SSE errors raise SDK
`APIError`. Witness delivery failures raise SDK connection/timeout errors with
the original `WitnessError` as their cause.
Background generation and raw SDK transport options are unsupported.

`stream=True` returns a typed event iterator with close/context-manager support.
The full SSE capture is verified and durable before the iterator becomes
available. Event contents/order and iterator observations are reproducible;
provider token timing and incremental network delivery are not exposed.
Default calls assemble the terminal Response, including subscription output items
sent before a terminal event. Credentials remain outside model parameters.

The canonical request includes provider normalization: `stream=true`, default
`tools=[]`, default `service_tier=default`, and subscription `store=false` plus
string-input conversion. JSON object key order is ignored; arrays, null versus
missing, images, tool arguments and other values retain their meaning. API and
subscription routes are distinct. The optional bounded subscription `session_id`
routing header is included in matching and checked against the TLS authorization.

The logical key is `(provider, canonical-body SHA256, request_id, session_id)`
inside an authenticated allocation scope `(deployment, round, miner)`.
`request_id` is an integer, default zero. Equal keys reuse the retained outcome,
including concurrent duplicates and reuse across prompts. A different identifier
permits fresh inference for an otherwise identical request. Serialized request
keys and call hashes use the `request_id` field.

The gateway selects its current round at allocation/reconnect and returns it in
allocation replies. `POST /v1/pods/start` accepts a start UUID and optional predecessor;
miners need no round configuration. Per-call requests contain no round, prompt,
image hash, generation seed, source hash, URL or global invocation ordinal.
The server owns round context for access deadlines, state and evidence.
`ArtifactClient.load_context()` reads the approved source commit through the
authenticated `GET /v1/artifacts/context` endpoint.
Preview requests supply source and preview options without creating stored
artifacts. After `generate` returns, the runner submits its JavaScript or `None`
with the prompt hash and seed. The Pod makes the fixed final renders and supplies
the round in storage paths. The approved source commit is read for final checkpoint checks.
Final-save checkpoint reuse checks the prompt hash, seed, renderer and refreshed
approved source commit.
Source, prompt, seed and checkpoint checks remain required.

### Capture, failures and restart

`POST /v1/calls` starts or joins a logical call. Its JSON body contains `request`,
an object with `provider`, the normalized request-body `request_sha256`,
`request_id`, and optional subscription `session_id`. A new or unfinished call
returns `202 Accepted` with
the same durable exchange, a `Location: /v1/calls/<exchange_id>` header and a
`Retry-After` hint. An assigned call includes its proof/capture WebSocket session;
HTTP acceptance alone cannot authorize a provider request. A completed call
returns `200` with its saved completion metadata, including provider failures.
An expired unclaimed assignment can renew its deadline for the same exchange,
fencing its previous claim token. Claimed captures retain their original deadline.
`GET /v1/calls/<exchange_id>` returns the current snapshot. The supplied client's
`responses.create()` handles polling and retrieval and returns a Response or
buffered stream, or raises a provider, connection or timeout error. Miners do not
need to call `resume()` for ordinary calls. A capture/recovery wait is bounded by
`delivery_timeout` and the exchange deadline; REST requests time out after thirty
seconds.

A logical call owns a current capture attempt; an exchange UUID identifies an
attempt, and a private capture capability fences its selected Pod/start. CAS state
allows only one claim and authorization. The verified request body/header must
match the reserved key before any billable HTTP bytes leave. Frozen model/tier
eligibility remains enforced; live per-prompt admission is absent.

Authorization and a possible-dispatch ledger entry become durable before release.
A missing acknowledgement can mean dispatch occurred. It never permits a fresh
paid request. Recovery finalizes retained ciphertext or joins the same exchange;
an unresolved paid attempt keeps unknown usage and its retained spool. Losing
unuploaded spool bytes can prevent completion. Only durable non-dispatch evidence
permits an internal replacement attempt; earlier attempts remain addressable.
To recover setup, the same `POST /v1/calls` body includes `expected_exchange_id`.
The Pod atomically creates and assigns a replacement only when that exchange is
still current, durably failed, proven undispatched and unauthorized. Delayed or
duplicate recovery requests return the current attempt without replacing it.
The client allows two such replacements per `create()`, with one/two-second delays,
within the same capture/recovery wait window.
No inference retry follows a provider rejection or an ambiguously dispatched call.

The worker captures independently of client delivery and saves the response,
verified usage, manifest and settlement before issuing a result. Delivery outages
retry retrieval. If an outage escapes to submitted code, its exact witnessed error
is retained by the execution host, separately from the provider outcome. Replay
restores that observation even if delivery would now succeed. It never replaces
unknown usage with zero or manufactures a provider response for missing evidence.

The host saves observation entry before attempting work and its outcome before
returning to the caller. A process restart resumes saved observations and pending
logical calls. A top-level exception aborts that execution attempt; its trace is
retained separately and a subsequent attempt may recover the same paid calls.
Completed traces and their visible failures cannot be rewritten by a restart.
`resume(exchange_id)` retrieves a durable Response without repeating inference.

### Observation order and validator accounting

Each invocation has a per-logical-key occurrence number, independent of other
calls' arrival order. Concurrent identical keys are indistinguishable at this
boundary. Code requiring different branch-specific observations must supply stable,
distinct request IDs; the evidence cannot reconstruct thread roles from equal
arguments. Stream `next`/`close` operations also have observation
identities. Entry/exit event positions preserve causal overlap and delivery order;
they do not identify a model request. Replay permits overlapping invocations to
arrive in a different order, waits for their recorded predecessors, and rejects
missing/extra/changed operations, mismatched stream use or impossible order within
its timeout. Final JavaScript/None must match exactly. Ordering is enforced at
this boundary, not at arbitrary subsequent Python instructions. Programs whose
output still depends on uncontrolled scheduling fail output reproduction.

Only the expected prompt's execution observations are required. Other captured
work in the round remains available and need not be consumed by that replay.
Prompt attribution comes from running the expected submitted program and inputs,
not from client labels, body hashes or URLs. The validator charges distinct logical
calls consumed by that execution. Repeated use of one retained call in a replay
is charged once. Each different prompt consuming the same retained call is
independently charged its full verified cost; reuse does not transfer allowance.

A proven pre-dispatch failure has no paid dispatch charge. A provider failure or
lost delivery after possible dispatch counts once and needs verified usage.
Missing usage remains unknown and prevents budget acceptance, even for HTTP
rejections. Exactly the frozen call/credit limit is accepted; exceeding either
is rejected. Pricing uses Decimal arithmetic, full input weight including cached
tokens, output/reasoning once, and the existing long-context tier. Failed and
discarded candidates count if their calls were consumed. Replay has no inference
client and never appends evidence to satisfy missing work.

Example (letters denote logical keys, not global sequence numbers):

```text
original: enter A[0], enter B[0], deliver B[0]=HTTP failure,
          deliver A[0]=Response, enter C[0](A output), deliver C[0], output JS
replay:   B[0] may arrive before A[0]; both must enter before B's recorded
          delivery; restore the same failure, then A, then C and identical JS
other U:  captured elsewhere in the round; absent from this execution
reuse A:  A[1] gets the saved outcome; A's verified cost is charged once here
```

### Retained field inventory

All client-supplied request fields are claims until matched to verified provider
bytes. Execution observations are trusted host evidence, not TLS-attested execution.
The verified bundle and its descriptor must remain in trusted validator storage.

| Retained fields | Producer and trust | Storage and consumer | Requirement lost if removed |
|---|---|---|---|
| deployment, round, miner/scope key | Authenticated allocator/operator | Scoped R2 keys, grants, session/manifest; gateway, worker, exporter | Cross-miner/round isolation, access deadline and evidence binding |
| provider | Client claim, verified TLS host/path | RequestKey, execution, session; capture/replay | API and subscription requests could match different routes |
| request_sha256, request_id, optional session_id | Client key; body/header verified before dispatch | Call records, manifest; deduplication/replay | Changed work could reuse a capture; identical fresh requests could not be distinguished |
| actual request JSON | TLS request verifier | Private request.json and authorized ciphertext; exporter | Cannot check full parameters or derive requested model and canonical hash |
| exchange_id, historical attempts | Server UUID/CAS history | Call document/index and evidence directory; recovery/export | Cannot locate/fence a particular attempt or preserve safe retry history |
| phase, authorized, not_dispatched, failure | Worker/control writes, never miner assertions | CAS call state; capture, recovery, export | Cannot distinguish reusable result, safe retry and ambiguous paid dispatch |
| deadline | Server reservation clock | Call/local job state; worker/client | Cannot bound capture lifetime across reconnects |
| start_id, pod_id, capture_id | Allocator/selected worker; private claim fence | Call state (capture_id private); authorization/recovery | Old allocation or claimant could authorize a replacement |
| revision, version and conditional object ETag | Server/model schema and R2 CAS | Call/usage journals; state readers | Cannot distinguish schema or concurrent writes; revision prevents ABA |
| manifest_sha256 and request/response byte hashes | Sealed worker evidence plus native re-verification | Call/manifest/trusted bundle; downloads/replay | Cannot detect replacement or altered retained bytes |
| client/server/authorization ciphertext, credential claim, session | TLS capture and hidden-credential proof | Private evidence/spool; native verifier | Cannot authenticate request/response, hidden credential or allocation binding |
| certificate_time_unix_ns, proof_key_sha256 | Verifier plus sealed operator context | Manifest; replay verifier | Later certificate expiry/key changes could invalidate or misverify captures |
| artifacts: object key, SHA256, size; signed URL | Authenticated Pod descriptor, scoped R2 signer | Manifest/retrieval reply; artifact client | Cannot retrieve bounded evidence from the intended immutable object |
| pricing_sha256, model, charge state, usage, credits | Frozen operator policy, verified request/usage | Per-call dispatch journal and verified bundle; settlement/budget | Cannot price consistently, fence uncertain dispatch or distinguish unknown from zero |
| provider_status and response SSE | Authenticated provider HTTP/TLS | Manifest/response.sse; SDK decoder | Cannot reproduce results, errors, event contents, refusals or failed Responses |
| image_sha256, generation seed | Execution host's frozen inputs; validator rechecks expected inputs | Local execution.json; execution host/replay | Cannot select/resume/check the same execution inputs; never call attribution |
| occurrence, action, index, stream | Execution boundary | Execution observations; replay | Cannot distinguish repeated invocations and stream operations using one capture |
| entered, exited, outcome | Execution boundary's serialized event clock | Execution observations; replay scheduler | Cannot restore causal overlap/visible delivery or detect incomplete work |
| observed exchange_id and failure message/code/status/source/pending | Execution boundary; exchange checked against operator call state | Execution observations; replay | A delivery/infrastructure failure could disappear or become a different exception |
| finished, aborted, output_sha256, output_is_none | Execution host | Execution observations; restart/export/replay | Cannot distinguish crash/abort/completion or compare None versus empty JS |
| execution/pricing hashes and captures in verified.json | Operator exporter after native checks | Trusted local bundle; ReplayWitness | Cannot bind the validated observations to the retained outcomes and accounting |

Derived: call IDs from keys, first exchange ID from scope/key, artifact paths,
requested model, terminal Response, typed events and prompt totals. No prompt
allowlist, live prompt ledger, generation-owned call table, global invocation
cursor, descriptive URL, source-labelled compatibility object or miner recording
lifecycle is needed. Call/usage state uses v2 keys. Existing v1 evidence is not
converted or deleted; its pinned client/exporter remains necessary to read it.
Finish old active captures with the matching pinned stack. Use matching v2 client,
worker and gateway sources in a new round/deployment scope for this incompatible
state/API cutover; these local changes are not a deployment.

`WITNESS_TIMINGS=1` enables connection diagnostics across the client, gateway and
worker. Records include call IDs, message kinds, byte counts, send/receive times,
close codes and fixed failure stages. Message contents, credentials and exception
messages are not logged. Gateway transport and worker diagnostics use bounded
queues so a stalled log sink cannot block traffic; diagnostic lines are dropped
when a queue fills.

During capture and recovery, after five seconds without worker progress,
`witness_worker_stall` records the active phase, Pod, exchange and thread IDs, and
up to 32 stack frames, starting at the current operation. Frames contain file
names, function names and line numbers; locals, arguments, source lines and
exception contents are excluded. Linux also reports the thread's kernel wait
function when available. A separate bounded
output queue retains up to 128 stack reports during routine log backpressure.
Sampling is limited to three reports per pause, five seconds apart. Connection
polling resets the timer; intentional upload retry sleeps have a 60-second
threshold. The watchdog requires the process to be able to schedule its Python
sampling thread.

## Provisioning and lifecycle

Miner HTTP requests and WebSocket upgrades use `btauth/1`, canonical sr25519
hotkeys, ten-second nonces and ownership checks. A Python refresher checks the
Finney genesis, one finalized block, its timestamp and every subnet hotkey. The
complete membership snapshot and gateway nonce cache stay in gateway memory.
Absent/stale membership fails closed. The optional test hotkey is restricted to
`test-0`, `test-1`, and `test-2`.

A deliberate connection uses a fresh start UUID and names its predecessor.
Lifecycle states are suffixes of the RunPod name: pending, active, draining and
drained. Immutable environment markers include ownership, the connection epoch,
predecessor, pinned specification and dormant boot policy.

Idle retirement revokes the durable R2 selection fence and marks the actual old resource as draining before asking the worker
to stop accepting work. Capture and evidence uploads must finish before an
authenticated worker reports permanent drain. Only then is an idle resource marked
drained and deleted. A new miner allocation revokes and terminates that miner's
existing worker Pods, including previous rounds, without waiting for a worker drain
acknowledgement. Unresolved calls stay unresolved. Creation of the replacement
requires an exact-ID GET 404 after DELETE.

The gateway serializes worker create admission and records requests in R2 before
creating Pods. Delivery retries join the same allocation across gateway restarts.
On restart, RunPod inventory is queried directly. The client also recovers when
its previous Pod ID returns 404. Installed clients can discover and reattach the
current allocation through `GET /v1/pods/current`; a new `POST /v1/pods/start` request
joins the queue for a fresh Pod. R2 retains the selection fence; RunPod inventory
remains authoritative for identity, capacity and lifecycle.

Allocation requests support `Prefer: wait=N` for a readiness wait of up to twenty
seconds per HTTP request. The response returns as soon as the selected Pod reports
ready and its direct TLS access grant is available. A pending response supplies
zero in `retry_after`; the client continues waiting for the same allocation
within its delivery timeout. Renewing access validates the current selection
before returning an endpoint. Rate-limit errors retain their `Retry-After` header.
Requests without this preference return immediately with the configured polling
interval. Pod startup does not repeat for subsequent model calls or renders.

All copies boot dormant. A confirmed active copy wins over a late dormant copy of
the same connection; newer connection epochs supersede older ones. Cleanup adopts a sole
valid late create, and drains superseded or duplicate resources before deleting
them. Durable R2 inference claims still prevent another paid dispatch of a call.

New Pods start dormant and connect outbound for lifecycle control using a per-start
capability. The gateway durably selects the exact Pod ID. The Pod checks this
selection before claiming a capture and before releasing a provider request;
draining Pods retain control
access to report completion. Readiness requires the expected proof-key
and frozen-pricing hashes. RunPod credentials stay on the gateway/revivor;
witness Pods receive a per-start control capability and private evidence-storage
credentials. The miner's OpenAI credentials remain hidden by the existing
credential proof.

The worker listens on `8000/tcp` with its own private CA and certificate for
`witness.internal`. The gateway returns that CA through authenticated HTTPS,
along with the provider-reported public IP and mapped external port from an
exact-ID Pod GET. HTTP and WebSocket clients connect to that address while
verifying `witness.internal` through SNI. The client receives a ten-minute
allocation grant bound to deployment, round, miner, start and Pod, and signs
every direct request with its hotkey. The grant cannot authenticate Pod control.
Current registration is rechecked when the grant is renewed.

Calls and proof messages use the direct Pod port. Outbound control messages carry
activation, readiness and drain only. A control reconnect does not cancel a paid
capture or revoke an otherwise selected healthy Pod. New calls stop at the round
deadline. Pre-deadline Pods drain then; allocations acquired after the deadline
serve recovery only and drain after inactivity. Old-round cleanup retires all
exactly owned Pods.

RunPod's [CPU create contract](https://docs.runpod.io/api-reference-v2/pods/create-a-pod)
accepts TCP ports with CPU compute; the [port guide](https://docs.runpod.io/pods/configuration/expose-ports)
describes public-IP forwarding and dynamic external ports. The selected shape is
Secure Cloud `cpu3c`, two vCPUs, with `8000/tcp`. A missing public mapping keeps the
allocation unready. This path requires direct TCP; it does not use RunPod's HTTP
proxy. Validate the exact image, shape, datacenter and mapped port on RunPod
before deployment; documentation and local fixtures do not establish live reachability.

## Evidence downloads

Private journals and spool uploads use the existing `rounds/<round>/<hotkey>/<exchange>/`
paths. Completed public evidence has immutable, content-addressed copies under
`evidence/v1/<deployment>/<round>/<hotkey>/<exchange>/<sha256>/<name>`. Execution observations stay with the execution host, outside miner scratch space.
The Pod returns the trusted manifest and expected hashes through its authenticated
TLS service and issues short-lived, object-specific signed R2 GET URLs. The client
downloads directly from R2, with a separate HTTP client that sends no hotkey or
allocation credentials, and verifies size, SHA256 and call bindings.

Normal completion checks the locally held request and received HTTP response
against the trusted manifest after durable accounting and evidence completion.
It does not download either payload again. Recovery downloads only missing bytes
from the artifact origin. Private capabilities, TLS keys, credentials, call/usage
journals and pending spools are never included in public evidence. Keep the bucket
private; signed object access does not require a public bucket or a gateway relay.

Pods permanently drain after at least 30 minutes without work, once capture and
upload counts reach zero. The gateway/revivor confirms provider termination;
exiting Python alone is not confirmed resource deletion. Cleanup requests drain
for retired/old-round Pods and never identifies ownership from a name prefix.

## Gateway recovery and hostname

Run Caddy and the revivor on an independent stable host and point the hostname
there. The revivor probes the gateway's direct TCP endpoint with a private CA and
fixed TLS server name. A DNS/frontend outage alone does not replace a healthy
gateway. A new gateway gets ten minutes of boot grace; after readiness, three
consecutive failures trigger replacement. Configuration/image changes also
trigger replacement.

Recovery marks the old gateway draining in RunPod, terminates it and confirms
absence before creating the new gateway. Witness captures continue independently
through the gap. After the new gateway is healthy, the revivor atomically updates
Caddy's upstream. Both public HTTPS/WSS and upstream TLS verify certificates.

Capacity admission counts actual managed RunPod workers, including pending,
duplicate, draining and EXITED resources until deletion is confirmed. It uses
fresh inventory at admission plus durable headroom for unresolved creates,
identified by deployment, round, hotkey and start ID. `WITNESS_MAX_PODS` sets the
worker count limit. RunPod hourly costs remain available as reporting metadata.

Each registered hotkey has at most one queued connection intent. Repeated starts,
including fresh IDs from other clients, return that intent without changing its
FIFO position. When its turn arrives, an intent terminates the miner's existing
worker Pods and creates a fresh Pod, regardless of their health. There is no
per-miner replacement budget or cooldown. Intents share a global minimum create
interval of
`GATEWAY_CREATE_INTERVAL_SECONDS` (default 10). `GATEWAY_MAX_QUEUE` bounds waiting
intents (default 1024).

Unsubmitted intents are canceled when their hotkeys lose eligibility. Submitted
creates retain their capacity headroom until reconciled. Each background pass
bounds allocation, retirement and access-grant work independently, so queued
starts do not postpone ready workers or access renewal.

The R2 journal at `pod-admission/v1/<deployment>.json` records authorization before
the provider POST. If the POST or authorization write has an uncertain outcome, the
gateway waits for the original Pod to appear; it never repeats the create merely
because a timeout or restart elapsed. That unresolved create continues
to count against capacity. An operator must reconcile provider inventory before
manually clearing an unresolved admission record. Confirmed lost Pods release
their physical capacity. Known terminal outcomes retry journal completion without
another provider POST. Request IDs, including requests joined to an existing queue
entry, remain idempotent for the round after later allocations and gateway restarts.

One gateway coordinates worker creation; a host file lock allows one revivor
process on the independent host. Multiple supervisor hosts are not supported.
Read-only selection checks share a two-second in-memory RunPod inventory cache;
create admission and cleanup explicitly refresh it. Mutable names are updated
with name-only PATCH requests; worker secrets and container settings are preserved.

Allocation and status requests without a wait preference use cached inventory
and grants. Readiness waits request access validation from background reconciliation,
which refreshes inventory and issues access grants. Gateway liveness probes remain
responsive during a provider outage; `admission_ready` requires loaded inventory
and a fresh admission fence.
Each authenticated hotkey receives two immediate requests, then one request per
`GATEWAY_REQUEST_INTERVAL_SECONDS` (default 10). `GATEWAY_MAX_REQUESTS` bounds active
body reads and authentication work (default 64); signature verification has four
slots and bounded replay tracking. Each hotkey has at most one active request;
readiness waits retain that slot after authentication. Bodies are limited to
16 KiB with a five-second read deadline.

Allocation replies without a wait preference advertise `GATEWAY_POLL_SECONDS`
(default 30). Clients retain the accepted start ID across network failures and
honor HTTP `Retry-After` on rate-limit and temporary-unavailability errors. Healthy
direct Pod calls continue independently of gateway polling. Busy workers provide
bounded local backpressure. RunPod stock and numeric Pod-count quota are not
guaranteed. Cleanup drains excess resources, preferring dormant copies over active
workers, and counts them until confirmed deletion.

## Pod image and renderer

Build the ordinary Pod image from the repository root:

```sh
docker build --platform linux/amd64 --target production \
  -f witness-server/worker/Dockerfile.pods -t witness-pods:local .
```

The image contains the matching witness client, gateway, worker and verifier,
plus render-service-js 0.4.10 with PMREM caching and bounded diagnostics. Node
22, Chromium 155, Python 3.14.9 and the base image digests are pinned in the
Dockerfile. The default entrypoint is `python -m pod_gateway`.

The worker role starts the renderer before connecting to the gateway. It uses
UID/GID 1001, an isolated environment without witness credentials, one rendering
slot and one validation slot. The execution budget is five seconds; rendering
has a thirty-second deadline. SwiftShader respects the CPU quota and assigned
CPU IDs. The local renderer listens on port 8002, with an eight-request queue
and a two-minute queue-wait limit. No renderer port is published by Pod allocation.
Gateway, revivor, export, certificate and help commands do not start Chromium.

Renderer startup has a sixty-second readiness deadline. A Node process exit
drains the worker while existing captures finish; worker exit stops the renderer
process group. Automatic recovery of the request interrupted by a browser crash
is not established by this lifecycle handling.

`render-benchmark` remains an explicit image target with the isolated benchmark
harness and fixtures. The immutable benchmark pins below describe their recorded
images; they do not identify a new build from the current source.

## Pinned witness with renderer

The shared validated benchmark baseline is **`witness-renderer-20261023`**.
Its source of truth is
[`deploy/witness-renderer-20261023/manifest.json`](deploy/witness-renderer-20261023/manifest.json)
at Git tag `witness-renderer-20261023`. Use that named baseline when coordinating
work across chats. Keep its image, bootstrap, renderer, verifier and limit pins
unchanged; create a different baseline ID and tag for a runtime change.

The complete runtime is the manifest's **image digest plus its exact
[`bootstrap.py`](deploy/witness-renderer-20261023/bootstrap.py)**. Mount the
bootstrap read-only at `/opt/witness-baseline/bootstrap.py` and use the recorded
Python entrypoint. The bootstrap checks the expected image sources before
applying its fixed configuration and worker opening fix. The image alone does
not contain that fix. The manifest's runtime file hashes are authoritative;
rebuilding from the current branch does not reproduce the pinned renderer.

For a credential-free configuration check, start the pinned image with
`--network none` and `BENCH_CHECK_ONLY=1`. The bootstrap compiles the configured
sources and prints their hashes without starting the benchmark or inference.
Keep credentials and evidence volumes outside this baseline.

The reference configuration uses three preparation slots, twelve model-call
slots, eight prompt pipelines and one rendering job. It completed 128 prompts,
256 offline replays and 1,278 exact PNG comparisons. This full run used an amd64
container emulated on a Mac; it does not establish full-batch native RunPod
performance or production allocation/revivor behavior. Successful PNGs matched;
resource timeout outcomes remain load-sensitive and one unpaid connection
required recovery. The manifest records these limits.

The bootstrap's reference round is `test-3`. Preserve its saved recordings and
accounting. New inference needs a fresh data namespace and a new run
configuration; do not append a new experiment to the reference run. Retain the
pinned image digest when cleaning up benchmark images.

### Authorization notification recovery pin

For recovery validation, use **`witness-renderer-20261023-auth-notify`** and its
[`manifest.json`](deploy/witness-renderer-20261023-auth-notify/manifest.json) and
[`bootstrap.py`](deploy/witness-renderer-20261023-auth-notify/bootstrap.py).
The image digest is shared with the benchmark reference; this exact bootstrap
is required. It makes the live authorization acknowledgement best-effort after
durable authorization, so losing that notification cannot abort the approved
capture. The manifest pins the post-bootstrap relay and channel source bytes.

The compatible client source remains `5b5b68ba11246a20303bbc08f094fa90f4308398`.
A client prover needs the full matching public v1 proof-parameter set and a
platform-compatible ABI 2 library. An interrupted client retrieves the saved
exchange; another authorization is not evidence of safe recovery. Other failed
capture retry behavior and renderer timeout limits retain their existing scope.
The renderer CPU-quota startup candidate is not part of this runtime. The
manifest distinguishes the reference benchmark from this pin's local recovery
validation; native RunPod recovery validation is still required.

## Python 3.14.9 Pod runtime pin

**`witness-pods-20261023-python314`** pins the ordinary Pod worker and gateway
image to Python 3.14.9. Its
[`manifest.json`](deploy/witness-pods-20261023-python314/manifest.json) and Git tag
`witness-pods-20261023-python314` identify the image, application sources,
dependencies and local validation. It uses `python -m pod_gateway` directly and
requires no bootstrap.

The local image tag is `witness-pods:witness-pods-20261023-python314`. The manifest's
`image` records its local digest; `publication_target` records the intended
private registry tag. Registry publication is pending. RunPod configuration needs
the published registry digest in `WITNESS_IMAGE` and, when explicitly set,
`WITNESS_GATEWAY_IMAGE`.

## Build and configuration

Build from the repository root for the target CPU architecture:

```sh
docker buildx build --platform linux/amd64 --load \
  -f witness-server/worker/Dockerfile.pods -t witness-pods:local .
```

The isolated renderer benchmark uses a separate target:

```sh
docker buildx build --platform linux/amd64 --load --target render-benchmark \
  -f witness-server/worker/Dockerfile.pods -t witness-render-bench:local .
```

It runs Python capture and one Node.js renderer in the same Pod. Chromium and its
Debian package snapshot are pinned in the Dockerfile. The benchmark entrypoint
requires `RUNPOD_POD_ID`, `BENCH_ADMIN_TOKEN`, `BENCH_END` (Unix seconds),
`BENCH_DEPLOYMENT`, `BENCH_HOTKEY`, `BENCH_START_ID` and `BENCH_IMAGE`.
`BENCH_ROUND` defaults to `test-0`; the existing test-hotkey round restrictions
apply. Use a fresh deployment, start ID and hotkey for each run.

The authenticated admin API listens on port 8001. `POST /boot` accepts the private
TLS files, R2 configuration, hourly price and phase; it waits for
renderer readiness before starting capture. The allocation gateway listens on
TLS port 8004, the direct witness on TLS port 8000, and the admin API is also
available on TLS port 8443. Expose `8004/tcp` and `8000/tcp`; the direct mapping
uses `RUNPOD_PUBLIC_IP` and `RUNPOD_TCP_PORT_8000`. Point the miner's gateway
configuration at the public mapping of 8004. These benchmark roles share a Pod
and do not establish shared gateway scaling or production networking acceptance.
Node runs as
UID 1001 without the Python process's storage credentials. Node and Chromium load
a scoped SwiftShader affinity wrapper that preserves the assigned CPU list for
the driver's default CPU-count mask. The benchmark sets only `ThreadCount` in
`SwiftShader.ini`; it does not configure an explicit affinity mask. The default
build continues to start `python -m pod_gateway`.

Publish through the operator's release process and configure the immutable image
digest in [deploy/revivor.env.example](deploy/revivor.env.example). Set the actual
deadline, canonical operator hotkey, capacity limits, keys and
HTTPS origin. Keep the deployment namespace/master key stable for the round.
Both roles must use the image's frozen pricing bytes. Images must use digests.
Gateway and worker state protocols must match. Finish active captures and replace
both roles before resuming miner traffic with an updated state protocol.

On the independent host, install `requirements-pods.txt` in a private virtualenv
and put `witness-client/src`, `miner-runtime/src` and `renderer-client/src` on
`PYTHONPATH`. Run commands from `witness-gateway`, as in the systemd example below.
Generate upstream TLS material offline with
`python -m pod_gateway certs /etc/witness/tls`. Set the base64
certificate chain/private key in the private environment and provide Caddy and
the revivor with the CA file. Leaf certificates last 30 days; renew them through
a reviewed configuration update.

Configure Caddy to start from `/var/lib/witness/Caddyfile` and permit the revivor
to reload its local admin endpoint. A bootstrap Caddyfile may initially return
503. The [systemd example](deploy/witness-revivor.service) keeps the revivor
independent of the gateway Pod. Starting it is a billable deployment step and is
not performed by installation.

## Verified offline replay

Export the trusted execution-host observation file with the operator's retained
capture store and exact frozen pricing bytes:

```sh
python -m pod_gateway export --deployment competition --round test-0 \
  --hotkey "$MINER_HOTKEY" --execution /private/host/execution.json \
  --output /private/replay/bundle
```

The exporter reads retained evidence and rechecks the credential/TLS proof using
the authorization snapshot, sealed certificate time and final ciphertext. It
compares the actual request, response, status, routing header and usage. Failed
infrastructure observations retain their operator call snapshot/partial evidence;
they cannot invent a provider result. Unknown usage can be exported and replayed,
but cannot pass prompt budget acceptance. `miner_runtime.validator.ReplayWitness(bundle)`
has no network, prover or inference client. Supply it to
`miner_runtime.validator.run_replay` with the expected frozen
image, seed, submitted code and reproducible renderer.

## Validation

Tests use in-memory R2/provider implementations and loopback TLS fixtures.
Gateway tests forbid external socket connections. No RunPod Pod or OpenAI
inference is created. Run gateway/client/worker suites, strict mypy, native and
verifier tests, and container import/health smoke checks before release.

References: [RunPod v2 contract](https://api.runpod.io/v2/openapi.json),
[R2 conditional PutObject](https://developers.cloudflare.com/r2/api/s3/api/),
[R2 consistency](https://developers.cloudflare.com/r2/reference/consistency/),
[Caddy upstream TLS](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

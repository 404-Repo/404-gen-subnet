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
| Assigned Pod | GET | `/v1/submissions` |
| Assigned Pod | GET | `/v1/submissions/{prompt}` |

The gateway's unauthenticated health check is `GET /health`. The Pod forwards
preview rendering to the internal renderer's `POST /render/grid` endpoint.

## Execution and replay contract

An async miner supplies `generate(image, seed, witness)` and awaits
`witness.responses.create(...)` and `witness.render(source, options)`. Synchronous
`generate(image, seed, witness, renderer)` programs remain supported. Model calls
need no prompt binding, recording handle or finish operation. The execution host
invokes the function; its witness boundary automatically observes calls for local
execution recovery. Candidate validation and
[manual replays](../validation-worker/MANUAL_REPLAY.md) run in
[validation-worker](../validation-worker/README.md).

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

`miner_runtime.runner.run_generation` is a development execution-host helper,
not a sandbox. The submitted function sees only the supplied clients. An actual
trusted host must keep evidence, expected outputs, observation state and the
replay engine outside miner access, including imports, native code and child
processes. The helper observes the witness boundary only; it does not implement
that isolation, a shared renderer/model scheduler, or the competition's complete
validation job.

The host helper remains synchronous. Invoke it outside an active asyncio loop
when running an async entry point. It runs that function in its own loop and
bridges its calls to the existing synchronous capture boundary and supplied
`Renderer`. Concurrent model calls and stream operations retain the same
execution observations. Live `AsyncWitnessClient` uses native async network I/O.
The host supplies rendering for both entry points; it may use live rendering or
verified render observations. This helper does not persist preview renders.

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
`request_id` is an integer, default zero. Equal keys reuse a retained successful outcome,
including concurrent duplicates and reuse across prompts. A different identifier
permits fresh inference for an otherwise identical request. Serialized request
keys and call hashes use the `request_id` field.

The gateway selects its current round at allocation/reconnect and returns it in
allocation replies. `POST /v1/pods/start` accepts a start UUID and optional predecessor;
miners need no round configuration. Per-call requests contain no round, prompt,
image hash, generation seed, source hash, URL or global invocation ordinal.
The server owns round context for access deadlines, state and evidence.
`ArtifactClient.load_context()` reads optional source-commit metadata through the
authenticated `GET /v1/artifacts/context` endpoint. `submission_commit` is null
when no commit is configured for the assigned miner; it does not authorize saving.
Preview requests supply source and preview options without creating stored
artifacts. After `generate` returns, the runner submits its JavaScript or `None`
with the prompt hash and optional seed. The Pod makes the fixed final renders and supplies
the round in storage paths. The SDK recovers retained model responses and
submitted prompt identities from the authenticated Pod without generated local
recovery files. Configuration, credentials, wallet material and proof parameters
remain caller-provided inputs.

Legacy final-save checkpoint reuse checks the prompt hash, seed, renderer and
source-commit metadata, including its absence.

Live execution runners await `AsyncWitnessClient.submit(image, source)` after generation returns.
`POST /v1/submissions` accepts `generation.prompt_sha256`, `source` (JavaScript or
null), and the base64 `prompt_image` for JavaScript. An omitted `generation.seed`
uses the assigned Pod's configured round seed. Legacy callers may still supply
it; a mismatched seed is rejected. The route retains prompt admission, final
judging renders, embeddings, identical-result reuse and conflicting-result errors.
`GET /v1/submissions` returns `{"submissions": {"<prompt_id>": <result>}}`
from the assigned miner's durable result index for that Pod's round. The Pod's
frozen `prompt_ids` mapping resolves image SHA256 hashes to the official filename
IDs used by the submission collector. An empty
index returns an empty object inside `submissions`; saved failures are retained.
The response is bounded to a 16 MiB index plus its JSON envelope. Live runners use
`await AsyncWitnessClient.get_submitted()`
to resume a batch or recover a committed result after a lost save reply. Lookup
performs no inference or rendering and does not wait for an unfinished save.
Generation/replay witness stubs reject both submission operations; validator
replay does not query or save results to a Pod. If generation or submission fails,
the caller reports the failure and leaves the prompt pending for a later pass.
Repeating identical canonical requests and stable request IDs reuses recordings;
preview rendering can repeat and changed model requests can start new inference.

Retained hash-keyed indexes recover under official IDs while preserving their
stored artifact pointers.

### Capture, failures and restart

`POST /v1/calls` starts or joins a logical call. Its JSON body contains `request`,
an object with `provider`, the normalized request-body `request_sha256`,
`request_id`, and optional subscription `session_id`. A new or unfinished call
returns `202 Accepted` with
the same durable exchange, a `Location: /v1/calls/<exchange_id>` header and a
`Retry-After` hint. An assigned call includes its proof/capture WebSocket session;
HTTP acceptance alone cannot authorize a provider request. A sealed attempt
returns `200` with its completion metadata, including evidence of provider errors.
Complete provider HTTP errors use the completed-capture envelope so existing
clients can raise the corresponding status exception. They remain failed outcomes
in call storage and are retryable.
Unstarted calls have no saved capture deadline. API replies use
`9007199254740991` as an effectively infinite integer deadline until authorization
saves the capture deadline immediately before provider release; its default window
is thirty minutes.
Joining or recovering an authorized capture retains that saved deadline.
`GET /v1/calls/<exchange_id>` returns the current snapshot. The supplied client's
`responses.create()` handles polling and retrieval and returns a Response or
buffered stream, or raises a provider, connection or timeout error. Miners do not
need to call `resume()` for ordinary calls. A capture/recovery wait is bounded by
`delivery_timeout` and the exchange deadline; REST requests time out after thirty
seconds.

Timeout terms:

- **SDK queue wait:** time waiting for local capture or preparation capacity;
  it consumes neither the capture window nor the initial delivery wait.
- **Setup timeout:** the worker's sixty-second ClientHello limit and five-minute
  authorization limit, measured from the start of its relay.
- **Capture deadline:** the persisted end of the provider-response capture window,
  established once at dispatch authorization.
- **Delivery timeout:** how long the SDK waits after acquiring initial capacity.
  Ending that wait does not cancel an already dispatched capture.

A logical call owns a current capture attempt; an exchange UUID identifies an
attempt, and a private capture capability fences its selected Pod/start. CAS state
allows only one claim and authorization. The verified request body/header must
match the reserved key before any billable HTTP bytes leave. Frozen model/tier
eligibility remains enforced; live per-prompt admission is absent.

Authorization and a possible-dispatch ledger entry become durable before release.
A missing acknowledgement can mean dispatch occurred. Recovery finalizes retained
ciphertext or joins the same exchange; an unresolved paid attempt keeps its
reservation and retained spool. Losing unuploaded spool bytes can prevent completion.
A replacement Pod recovers a completed saved response first. If its capturing Pod
was replaced, or its deadline expired with no active local capture, the unfinished
attempt fails and releases its reservation. A failed attempt permits a caller retry
with the same logical request and `request_id`.
An ordinary `POST /v1/calls` with the unchanged request starts the next attempt;
the caller needs no previous exchange UUID or retry handshake. The Pod atomically
replaces its current failed attempt, so concurrent starts join one replacement.
The optional `expected_exchange_id` remains accepted for existing callers.
A replacement has a new exchange UUID for its evidence, with the same logical key.
The logical call retains only its current attempt. Saved failure responses and
evidence remain under their own UUIDs for diagnostics, with the reason in
`failure.json`. Replaced attempts return
404 from the status endpoint.
Successful saved responses remain
reusable through client restarts and Pod replacement without another inference.
The client automatically allows two proven undispatched setup replacements per
`create()`, with one/two-second delays within the same wait window. Provider and
capture failures return to caller code; a subsequent `create()` retries the same
request ID. An in-flight capture remains joined until its outcome is known.
The server never starts another provider request from polling, recovery, or a
failed capture; another attempt requires a new caller start request.

The worker captures independently of client delivery and saves the response,
verified usage, manifest and settlement before issuing a result. Delivery outages
retry retrieval.

The host saves observation entry before attempting work and its outcome before
returning to the caller. A process restart resumes saved observations and pending
logical calls. A top-level exception aborts that execution attempt; its trace is
retained separately and a subsequent attempt may recover the same paid calls.
Completed traces and their visible failures cannot be rewritten by a restart.
`resume(exchange_id)` retrieves a durable Response without repeating inference.

### Generation recovery and validation

Generation helpers retain per-key observations for local execution recovery.
Candidate replay, evidence checks and per-prompt accounting belong to
[validation-worker](../validation-worker/README.md). Its
[manual replay guide](../validation-worker/MANUAL_REPLAY.md) describes request
matching, saved outcomes, pricing, reports and local caches.

### Retained field inventory

All client-supplied request fields are claims until matched to verified provider
bytes. Execution observations are trusted host evidence, not TLS-attested execution.

| Retained fields | Producer and trust | Storage and consumer | Requirement lost if removed |
|---|---|---|---|
| deployment, round, miner/scope key | Authenticated allocator/operator | Scoped R2 keys, allocation, session/manifest; gateway, worker | Cross-miner/round isolation, generation deadline and evidence binding |
| provider | Client claim, verified TLS host/path | RequestKey, execution, session; capture/replay | API and subscription requests could match different routes |
| request_sha256, request_id, optional session_id | Client key; body/header verified before dispatch | Call records, manifest; deduplication/replay | Changed work could reuse a capture; identical fresh requests could not be distinguished |
| actual request JSON | TLS request verifier | Private request.json and authorized ciphertext; replay | Cannot check full parameters or derive requested model and canonical hash |
| exchange_id | Server UUID/CAS assignment | Current call, exchange index and evidence directory; recovery/replay | Cannot locate/fence a capture or retrieve its saved outcome |
| phase, authorized, not_dispatched, failure | Worker/control writes, never miner assertions | CAS call state; capture, recovery, replay | Cannot distinguish reusable result, safe retry and ambiguous paid dispatch |
| deadline | Server reservation clock | Call/local job state; worker/client | Cannot bound capture lifetime across reconnects |
| start_id, pod_id, capture_id | Allocator/selected worker; private claim fence | Call state (capture_id private); authorization/recovery | Old allocation or claimant could authorize a replacement |
| revision, version and conditional object ETag | Server/model schema and R2 CAS | Call/usage journals; state readers | Cannot distinguish schema or concurrent writes; revision prevents ABA |
| manifest_sha256 and request/response byte hashes | Sealed worker evidence plus native re-verification | Call/manifest; downloads/replay | Cannot detect replacement or altered retained bytes |
| client/server/authorization ciphertext, credential claim, session | TLS capture and hidden-credential proof | Private evidence/spool; native verifier | Cannot authenticate request/response, hidden credential or allocation binding |
| certificate_time_unix_ns, proof_key_sha256 | Verifier plus sealed operator context | Manifest; replay verifier | Later certificate expiry/key changes could invalidate or misverify captures |
| artifacts: object key, SHA256, size; signed URL | Authenticated Pod descriptor, scoped R2 signer | Manifest/retrieval reply; artifact client | Cannot retrieve bounded evidence from the intended immutable object |
| model, charge state, usage, credits | Append-only operator model table, verified request/usage | Per-call dispatch journal; settlement/budget | Cannot price consistently, fence uncertain dispatch or distinguish unknown from zero |
| provider_status and response SSE | Authenticated provider HTTP/TLS | Manifest/response.sse; SDK decoder | Cannot reproduce results, errors, event contents, refusals or failed Responses |
| image_sha256, generation seed | Execution host's frozen inputs; validator rechecks expected inputs | Local execution.json; execution host/replay | Cannot select/resume/check the same execution inputs; never call attribution |
| occurrence, action, index, stream | Execution boundary | Local execution observations | Cannot resume repeated invocations and stream operations within a recorded generation |
| entered, exited, outcome | Execution boundary's serialized event clock | Local execution recovery | Cannot restore a generation's causal observations after a local restart |
| observed exchange_id and failure message/code/status/source/pending | Execution boundary | Local execution recovery | Cannot restore that local execution's observed error; offline replay does not use it |
| finished, aborted, output_sha256, output_is_none | Execution host | Execution observations; restart/replay | Cannot distinguish crash/abort/completion or compare None versus empty JS |

Derived: call IDs from keys, first exchange ID from scope/key, artifact paths,
requested model, terminal Response, typed events and prompt totals. No prompt
allowlist, live prompt ledger, generation-owned call table, global invocation
cursor, descriptive URL, source-labelled compatibility object or miner recording
lifecycle is needed. Call/usage state uses v2 keys. Existing v1 evidence is not
converted or deleted; its matching pinned stack remains necessary to read it.
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
hotkeys and ownership checks. Signed nonce timestamps are accepted from 30 seconds
old through 30 seconds ahead, including both boundaries. A Python refresher checks
the Finney genesis, one finalized block, its timestamp and every subnet hotkey. The
complete membership snapshot and gateway nonce cache stay in gateway memory.
Absent/stale membership fails closed. Automatic competition admission uses
Finney membership for every configured source branch.

A deliberate connection uses a fresh start UUID and names its predecessor.
RunPod display names use `w404-<deployment[:12]>-<round[:12]>-<hotkey[:8]>-<state>`.
Lifecycle states are pending, active, draining and drained. Display names can
repeat across miners and allocation attempts. Ownership and recovery use the
complete scope, full hotkey in `WITNESS_HOTKEY`, full allocation UUID in
`WITNESS_START_ID` and RunPod Pod ID. Immutable environment markers also include
the connection epoch, predecessor, pinned specification and dormant boot policy.

Idle retirement revokes the durable R2 selection fence and marks the actual old resource as draining before asking the worker
to stop accepting work. Capture and evidence uploads must finish before an
authenticated worker reports permanent drain. Only then is an idle resource marked
drained and deleted. A new miner allocation revokes and terminates that miner's
existing worker Pods. Replacement across rounds waits for a worker drain
acknowledgement so in-flight captures retain their evidence. Same-round deliberate
replacement retains its existing lifecycle. Unresolved calls stay unresolved. Creation of the replacement
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
ready and its direct TLS endpoint is available. A pending response supplies
zero in `retry_after`; the client continues waiting for the same allocation
within its delivery timeout. Rate-limit errors retain their `Retry-After` header.
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
access to report completion. Readiness requires the expected proof-key hash.
RunPod credentials stay on the gateway/revivor;
witness Pods receive a per-start control capability and private evidence-storage
credentials. The miner's OpenAI credentials remain hidden by the existing
credential proof.

The worker listens on `8000/tcp` with its own private CA and certificate for
`witness.internal`. The gateway returns that CA through authenticated HTTPS,
along with the provider-reported public IP and mapped external port from an
exact-ID Pod GET. HTTP and WebSocket clients connect to that address while
verifying `witness.internal` through SNI. The Pod verifies every direct request
against its assigned hotkey, including the signed method, path, body and nonce.
It does not require a gateway credential or recheck registration. Registration
is checked when allocating or replacing a Pod.

For older SDKs, allocation replies retain `access: "hotkey"` and
`expires: 253402300799` (the end of year 9999). These fixed compatibility fields
are not credentials; the Pod ignores bearer authorization. The gateway derives
the reply from allocation metadata and the Pod certificate, without storing
access grants. Older SDKs can attach and keep using that Pod without renewal.
Deploy the updated worker image before using this allocation protocol; an
existing worker image that requires grants must be replaced.

Calls and proof messages use the direct Pod port. Outbound control messages carry
activation, readiness and drain. A control reconnect does not cancel a paid
capture or stop normal Pod operations. The Pod enforces the fixed generation
deadline received at allocation, independently of the gateway. Status, saved
results and recording recovery remain available after that deadline. See [competition source and admission](README.md#competition-source-and-admission).

RunPod's [CPU create contract](https://docs.runpod.io/api-reference-v2/pods/create-a-pod)
accepts TCP ports with CPU compute; the [port guide](https://docs.runpod.io/pods/configuration/expose-ports)
describes public-IP forwarding and dynamic external ports. The selected shape is
Secure Cloud `cpu3c`, two vCPUs, with `8000/tcp`. A missing public mapping keeps the
allocation unready. This path requires direct TCP; it does not use RunPod's HTTP
proxy. Validate the exact image, shape, datacenter and mapped port on RunPod
before deployment; documentation and local fixtures do not establish live reachability.

## Evidence downloads

Private journals and spool uploads use `<storage-prefix>rounds/<round>/<hotkey>/<exchange>/`
paths. Completed public evidence has immutable, content-addressed copies under
`<storage-prefix>evidence/v1/<deployment>/<round>/<hotkey>/<exchange>/<sha256>/<name>`.
The empty storage prefix preserves official paths. Execution observations stay with the execution host, outside miner scratch space.
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
bounds allocation and retirement work independently. Returning an existing
endpoint does not schedule provider or authorization work.

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

Allocation and status replies use inventory and reported Pod readiness and TLS
certificates. Readiness waits are notified by background allocation reconciliation. Gateway liveness probes remain
responsive during a provider outage; `admission_ready` requires loaded inventory
and a fresh admission fence.
Each authenticated hotkey receives two immediate admission POST requests, then one
per `GATEWAY_REQUEST_INTERVAL_SECONDS` (default 10). An overlapping admission POST
returns `admission_in_progress`; an exhausted admission allowance returns
`request_rate_limited`. Allocation/status GETs do not
consume that allowance or hold the hotkey's admission slot.
`GATEWAY_MAX_REQUESTS` bounds active miner requests through response completion,
including readiness waits (default 64). Signature verification has four slots and
bounded replay tracking. Bodies are limited to 16 KiB with a five-second read deadline.

The gateway always writes JSON events to stdout. `gateway_request` records route
templates, status, fixed error codes, duration, authenticated identity, admission
limits, fence freshness and allocation readiness. Waiting allocations identify
the pending stage: allocation, Pod control, Pod readiness, certificate or endpoint.
Background failures record their stage and exception class; Pod control
connections/readiness have separate events. Headers, query
strings, request bodies, certificates, access tokens and exception messages are
excluded. A bounded output queue keeps a blocked log sink off request handlers;
the next accepted event reports `dropped_logs` after overflow.

Allocation replies without a wait preference advertise `GATEWAY_POLL_SECONDS`
(default 30). Clients retain the accepted start ID across network failures and
honor HTTP `Retry-After` on rate-limit and temporary-unavailability errors. Healthy
direct Pod calls continue without gateway polling. Busy workers provide
bounded local backpressure. RunPod stock and numeric Pod-count quota are not
guaranteed. Cleanup drains excess resources, preferring dormant copies over active
workers, and counts them until confirmed deletion.

## Gateway and witness Pod images

Build both images from the repository root:

```sh
docker build --platform linux/amd64 --target gateway \
  -f witness-server/worker/Dockerfile.pods -t witness-gateway:local .
docker build --platform linux/amd64 --target production \
  -f witness-server/worker/Dockerfile.pods -t witness-pods:local .
```

The `gateway` target runs `gateway` by default and also supports `revivor`,
`certs` and `publish-pricing`. It retains the TLS, membership and storage
dependencies, shared contracts and matching public verification key.
Pillow handles competition prompt images;
the image excludes Chromium, the renderer runtime, Torch, torchvision and
Transformers. The independent revivor still requires access to
the host's Caddy executable, admin endpoint, CA file and writable configuration.

The `production` target starts `python -m witness_pod`. It contains the matching
witness client, shared contracts, Pod package and verifier,
plus render-service-js 0.4.10 with PMREM caching and bounded diagnostics. Node
22, Chromium 155, Python 3.14.9 and the base image digests are pinned in the
Dockerfile. The gateway entrypoint is `python -m pod_gateway`.

The worker role starts the renderer before connecting to the gateway. It uses
UID/GID 1001, an isolated environment without witness credentials, one rendering
slot and one validation slot. The execution budget is five seconds; preview
rendering has a thirty-second deadline and each final-save render request has a
150-second deadline. SwiftShader respects the CPU quota and assigned
CPU IDs. The local renderer listens on port 8002. Its validation and rendering
pools queue waiting requests without a configured length limit. No renderer port
is published by Pod allocation.
Gateway, revivor, certificate and help commands do not start Chromium.

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
docker buildx build --platform linux/amd64 --load --target gateway \
  -f witness-server/worker/Dockerfile.pods -t witness-gateway:local .
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
build starts `python -m witness_pod`.

Publish both images through the operator's release process and configure their
immutable digests in [deploy/revivor.env.example](deploy/revivor.env.example):
`WITNESS_GATEWAY_IMAGE` selects the gateway for creation and revival;
`WITNESS_IMAGE` selects miner witness Pods. Both pins are required and are
forwarded to replacement gateways. There is no fallback between image roles.
`WITNESS_IMAGE` pins the Pod and its packaged renderer. Retain the approved
embedding-model revision for final-result saving.

Prepare the revivor source, dependencies and private configuration offline.
Perform these steps only at the approved rollout:

1. Finish active captures and stop the existing revivor. Upgrade its host
   checkout and virtualenv to this release using `requirements-gateway.txt`
   with `constraints-pods.txt`.
2. Set `WITNESS_GATEWAY_IMAGE` and `WITNESS_IMAGE` to their corresponding
   images from this release. Set `R2_REPLAY_BUCKET` and `R2_REPLAY_CDN_URL`,
   then restart the upgraded revivor. Confirm the replacement gateway is
   healthy and ready before resuming miner traffic.

Building these images does not change running Pods or pins.

Set the actual
competition repository and branch, embedding revision, canonical operator hotkey, capacity limits, keys and
HTTPS origin. Keep the deployment namespace/master key stable for the round.
Workers use the deployment's [shared model table](README.md#model-table), which
must be published before starting them. Model additions do not change Pod image
digests or allocation fingerprints. Images must use digests.
Gateway and worker state protocols must match. Finish active captures and replace
both roles before resuming miner traffic with an updated state protocol.

On the independent host, install `requirements-gateway.txt` with
`constraints-pods.txt` in a private virtualenv and put `shared`,
`witness-client/src`, `miner-runtime/src` and `renderer-client/src` on
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

## Validation

Tests use in-memory R2/provider implementations and loopback TLS fixtures.
Gateway tests forbid external socket connections. No RunPod Pod or OpenAI
inference is created. Run gateway/client/worker suites, strict mypy, native and
verifier tests, and container import/health smoke checks before release.

References: [RunPod v2 contract](https://api.runpod.io/v2/openapi.json),
[R2 conditional PutObject](https://developers.cloudflare.com/r2/api/s3/api/),
[R2 consistency](https://developers.cloudflare.com/r2/reference/consistency/),
[Caddy upstream TLS](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

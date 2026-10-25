# Python witness gateway and ordinary CPU Pods

The Python gateway authenticates miners, allocates one current witness Pod per
miner, and returns its direct TLS endpoint. Witness Pods own call reservation,
authorization, recovery, delivery ordering and generation recording. They perform verification and
provider capture; they never execute miner programs. An independent revivor
restores the gateway and cleans up exactly owned Pods. RunPod owns Pod identity,
lifecycle and capacity. Private R2 objects retain model-call records, usage and
evidence. SQL is not required.

Imports, installation, tests and `--help` do not create infrastructure.

## Identity, deduplication and order

The operator pins the round, frozen PNG bytes and generation seed.
These identify one `generate` run. A prompt URL is optional
descriptive metadata and is never fetched by the gateway. Keep private access
tokens out of descriptive URLs.

Within a generation, the request key covers the provider route, complete
normalized JSON request and `request_id`. Object key order is normalized;
array order, images, tools, missing versus null values and all other request
fields are preserved. API and subscription routes have different identities.

Identical requests share one exchange, including concurrent duplicates. One
worker atomically claims that exchange; other invocations wait for its durable
result. `request_id=1` requests a fresh exchange for otherwise identical input.
It is an integer call identifier, stays stable for recovery and replay, and is
not sent to the provider. It does not control sampling.
Fresh inference can still produce identical text.

The recording stores every invocation in start order, including cache hits, and
the order in which results become available to Python. Transport retries retain
the original identity. A restart consumes the saved generation prefix before
appending work. Changed requests in an existing prefix are rejected. A pinned
round prevents reconnects from silently moving work into another competition.

Prompt-scoped generations use v2 identities. Replay also reads source-labelled
v1 recordings with their original identities. Source identity belongs to the
submission executor and validator; the witness does not attest program execution.
Use matching client and gateway sources. Keep the matching v1 client for an
existing v1 batch restart, or retrieve its saved exchanges with `resume()`.
Use a fresh round namespace for a recording-format cutover.

```python
from witness_client import WitnessClient, run_generation

config = {
    "round": "test-0",
    "witness_url": "https://witness.example.org",
    "proof_parameters": "/runtime/params",
    "state_directory": "/private/recovery",
}
with WitnessClient(config, hotkey=hotkey, openai_api_key=api_key) as witness:
    source = run_generation(generate, frozen_png, seed, witness, renderer)
```

Miner code keeps `generate(image, seed, witness, renderer)` and
`witness.responses.create(...)`. Intentional fresh requests can use
`witness.responses.create(..., request_id=1)`.

## Capture and durability

The existing TLS gate and credential proof remain authoritative. Before paid
HTTP bytes leave the Pod, the worker verifies the actual request, compares its
hash to the reservation, checks the frozen model/service-tier policy, saves the
authorization and reserves the dispatch allowance. A supplied hash alone cannot
authorize a different request. Flex and models missing from frozen pricing are
rejected. Other service tiers are forwarded to the provider; competition usage
is charged at the frozen model rates for every admitted tier.

Clients queue TLS setup and credential proofs locally. Once authorized, model
calls can run in parallel while later requests prepare their proofs.

After release, capture is independent of the miner or gateway connection. The
full response, verified usage and manifest become durable before delivery. A
local spool supports upload/finalization retries without another provider
connection. Pending uploads keep the Pod busy. `resume()` and repeated
generation calls recover the original response, including structured provider
rejections, failed/incomplete Responses and tool/refusal output.

The prompt ledger spans generation seeds, source revisions, credentials and Pod
replacements. It uses exact frozen pricing bytes and Decimal arithmetic, counts
input tokens at their full published weight, includes output/reasoning once and
applies the long-context tier to the whole call. Possible dispatches consume the
call allowance once. Known credit exhaustion blocks further dispatch. Concurrent
calls can cross the credit limit, which remains a recorded breach. Unknown usage
stays pending, with no claimed remaining balance or guessed zero charge.

The client joins an active capture and reuses a completed response. A failed
capture can use up to two replacement attempts per `create()` call, with short
backoff. Each replacement has a fresh exchange ID; the generation, request key,
invocation position and existing delivery receipts stay the same. Failed attempts
and partial evidence remain available. A late attempt cannot authorize or replace
its successor's response. Delivered outcomes cannot be retried.

An authorized attempt with missing response evidence retains unknown usage and
consumes its dispatch allowance. Only durable evidence that an attempt did not
dispatch permits replacement inference. A Pod/host failure while a capture is still
unresolved is joined. Exhausted retries leave
the generation pending; restarting consumes its saved prefix and completed work.
After a worker process restarts, an incomplete authorized capture stays unresolved
with unknown usage and its original deadline. Its spool is retained. A complete
saved capture can be finalized without another provider request. Losing the Pod's
ephemeral spool also loses bytes that have not reached durable evidence storage.

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

Replacement revokes the durable R2 selection fence and marks the actual old resource as draining before asking the worker
to stop accepting work. Capture and evidence uploads must finish before an
authenticated worker reports permanent drain. Only then is the resource marked
drained and deleted. EXITED and unrecoverable ERROR Pods do not require a worker
drain acknowledgement; their unresolved calls stay unresolved. Replacement
requires an exact-ID GET 404 after DELETE.

The gateway serializes worker create admission and retains only short-lived
in-flight attempts in memory. A lost create reply is joined for up to ten minutes;
an expired attempt reports lost rather than claiming a Pod exists. On restart,
RunPod inventory is queried directly. The client also recovers when its previous
Pod ID returns 404. A healthy current allocation is reattached across client
restarts and transport reconnects. R2 retains a small selection fence; RunPod
inventory remains authoritative for identity, capacity and lifecycle.

A restarted coordinator can repeat a create whose result is still invisible.
Physical Pod creation is therefore not claimed to be exactly once. All copies
boot dormant. A confirmed active copy wins over a late dormant copy of the same
connection; newer connection epochs supersede older ones. Cleanup adopts a sole
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
`evidence/v1/<deployment>/<round>/<hotkey>/<exchange>/<sha256>/<name>`. Sanitized
recordings use generation- and hash-scoped keys under the same evidence prefix.
The Pod returns the trusted manifest and expected hashes through its authenticated
TLS service and issues short-lived, object-specific signed R2 GET URLs. The client
downloads directly from R2, with a separate HTTP client that sends no hotkey or
allocation credentials, and verifies size, SHA256 and invocation bindings.

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
fresh inventory at admission plus temporary headroom for unresolved creates in
that gateway process. `WITNESS_MAX_PODS` sets the worker count limit. RunPod hourly
costs remain available as reporting metadata. No capacity slot survives as a
phantom R2 reservation after Pod loss.

One gateway coordinates worker creation; a host file lock allows one revivor
process on the independent host. Multiple supervisor hosts are not supported.
Read-only selection checks share a two-second in-memory RunPod inventory cache;
create admission and cleanup explicitly refresh it. Mutable names are updated
with name-only PATCH requests; worker secrets and container settings are preserved.

Creates hidden across a coordinator restart can temporarily exceed the Pod-count
limit before appearing in inventory. Cleanup drains excess resources, preferring
dormant copies over active workers, and continues counting them until confirmed
deletion. RunPod stock and numeric Pod-count quota are not guaranteed.
Fleet FIFO/priority queuing remains
separate; busy workers provide bounded local backpressure.

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
TLS files, R2 configuration, prompt hashes, hourly price and phase; it waits for
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
deadline, canonical operator hotkey, frozen prompt hashes, capacity limits, keys and
HTTPS origin. Keep the deployment namespace/master key stable for the round.
Both roles must use the image's frozen pricing bytes. Images must use digests.
Gateway and worker state protocols must match. Finish active captures and replace
both roles before resuming miner traffic with an updated state protocol.

On the independent host, install `requirements-pods.txt` in a private virtualenv
and put `witness-client/src` on `PYTHONPATH`. Generate upstream TLS material
offline with `python -m pod_gateway certs /etc/witness/tls`. Set the base64
certificate chain/private key in the private environment and provide Caddy and
the revivor with the CA file. Leaf certificates last 30 days; renew them through
a reviewed configuration update.

Configure Caddy to start from `/var/lib/witness/Caddyfile` and permit the revivor
to reload its local admin endpoint. A bootstrap Caddyfile may initially return
503. The [systemd example](deploy/witness-revivor.service) keeps the revivor
independent of the gateway Pod. Starting it is a billable deployment step and is
not performed by installation.

## Verified offline replay

Use the pinned verifier/runtime to read and verify a private replay bundle:

```sh
python -m pod_gateway export --deployment competition --round test-0 \
  --hotkey "$MINER_HOTKEY" --generation "$GENERATION_SHA256" \
  --output /private/replay/bundle
```

Export only reads the bucket. It rechecks the credential/TLS proof using the held
authorization snapshot and final ciphertext, then compares request, response,
usage and manifest hashes. Certificate validation uses the exact sealed
authorization time, so later expiry does not invalidate a capture. Partial,
altered and unresolved recordings cannot become verified bundles.

`ReplayWitness(bundle)` has no HTTP/prover/inference client. It matches model
requests, restores invocation and delivery order across concurrent callers,
returns original provider outcomes, and checks final JavaScript/None and complete
consumption. Missing, extra, reordered or changed calls are findings. Impossible
orders fail within the replay timeout.

Keep the bundle and replay engine outside untrusted miner execution. The
`run_generation`/`run_replay` helpers are integration/development helpers, not a
process-isolation boundary. This layer records witness calls; the separate miner
sandbox, renderer replay and OS scheduling are outside it. The validator still
owns competition-wide reconciliation, including pending prompt usage from other
generations.

## Validation

Tests use in-memory R2/provider implementations and loopback TLS fixtures.
Gateway tests forbid external socket connections. No RunPod Pod or OpenAI
inference is created. Run gateway/client/worker suites, strict mypy, native and
verifier tests, and container import/health smoke checks before release.

References: [RunPod v2 contract](https://api.runpod.io/v2/openapi.json),
[R2 conditional PutObject](https://developers.cloudflare.com/r2/api/s3/api/),
[R2 consistency](https://developers.cloudflare.com/r2/reference/consistency/),
[Caddy upstream TLS](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

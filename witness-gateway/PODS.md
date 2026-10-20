# Python witness gateway and ordinary CPU Pods

The Python gateway authenticates miners, allocates one current witness Pod per
miner, and forwards bounded WebSockets. Witness Pods perform verification and
provider capture; they never execute miner programs. An independent revivor
restores the gateway and cleans up exactly owned Pods. Private R2 objects retain
allocation intents, model-call records, usage and evidence. SQL is not required.

This implementation is separate from the existing TypeScript/Serverless gateway.
Imports, installation, tests and `--help` do not create infrastructure.

## Identity, deduplication and order

The operator pins the round, frozen PNG bytes, source archive SHA256 and
generation seed. These identify one `generate` run. A prompt URL is optional
descriptive metadata and is never fetched by the gateway. Keep private access
tokens out of descriptive URLs.

Within a generation, the request key covers the provider route, complete
normalized JSON request and `witness_seed`. Object key order is normalized;
array order, images, tools, missing versus null values and all other request
fields are preserved. API and subscription routes have different identities.

Identical requests share one exchange, including concurrent duplicates. One
worker atomically claims that exchange; other invocations wait for its durable
result. `witness_seed=1` requests a fresh exchange for otherwise identical input.
It distinguishes witness requests and is not sent as a provider seed parameter.
Fresh inference can still produce identical text.

The recording stores every invocation in start order, including cache hits, and
the order in which results become available to Python. Transport retries retain
the original identity. A restart consumes the saved generation prefix before
appending work. Changed requests in an existing prefix are rejected. A pinned
round prevents reconnects from silently moving work into another competition.

```python
from witness_client import WitnessClient, run_generation

config = {
    "transport": "pods-v1",
    "round": "test-0",
    "witness_url": "https://witness.example.org",
    "proof_parameters": "/runtime/params",
    "state_directory": "/private/recovery",
}
with WitnessClient(config, hotkey=hotkey, openai_api_key=api_key) as witness:
    source = run_generation(
        generate, frozen_png, seed, witness, renderer,
        source_sha256=submitted_source_archive_sha256,
    )
```

Miner code keeps `generate(image, seed, witness, renderer)` and
`witness.responses.create(stream=True, ...)`. Intentional fresh requests can use
`witness.responses.create(..., witness_seed=1)`.

## Capture and durability

The existing TLS gate and credential proof remain authoritative. Before paid
HTTP bytes leave the Pod, the worker verifies the actual request, compares its
hash to the reservation, checks the frozen model/service-tier policy, saves the
authorization and reserves the dispatch allowance. A supplied hash alone cannot
authorize a different request. Only default/Standard and priority tiers are
admitted; Flex and unknown models are rejected.

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

A Pod/host failure before response persistence can leave a paid result
unrecoverable. Its exchange stays unresolved and is never silently inferred
again. An infrastructure error leaves the generation attempt unresolved even if
miner code catches it. A retry uses the same generation identity and saved
prefix. Completed work is never replaced by fresh inference.

## Provisioning and lifecycle

Miner HTTP requests and WebSocket upgrades use `btauth/1`, canonical sr25519
hotkeys, ten-second nonces and ownership checks. A Python refresher checks the
Finney genesis, one finalized block, its timestamp and every subnet hotkey.
Absent/stale membership fails closed. The optional test hotkey is restricted to
`test-0`.

A deliberate connection uses a fresh start UUID and names its predecessor.
Repeating it joins the same operation. The old selection is revoked before
drain. Capture and evidence uploads must finish before a permanent authenticated
drain seal permits deletion. EXITED Pods have no running capture process; their
unresolved calls stay unresolved. Replacement requires an exact-ID GET 404 after
DELETE, rather than relying on its acknowledgement.

The R2 transition to `creating` grants one caller permission to POST to RunPod.
Lost replies/crashes are reconciled by exact name, full identity markers, pinned
image and spec fingerprint. Empty inventory never authorizes another POST. An
uncertain intent can remain parked until its exact Pod appears. There is no
fabricated proof of non-dispatch or automatic create retry.

New Pods start dormant and connect outbound using a per-start capability. Only
the selected exact Pod ID may attach. Readiness requires the expected proof-key
and frozen-pricing hashes. RunPod credentials stay on the gateway/revivor;
witness Pods receive a per-start control capability and private evidence-storage
credentials. The miner's OpenAI credentials remain hidden by the existing
credential proof.

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

Recovery revokes the old gateway's R2 serving fence, terminates it and confirms
absence before creating the new gateway. Witness captures continue independently
through the gap. After the new gateway is healthy, the revivor atomically updates
Caddy's upstream. Both public HTTPS/WSS and upstream TLS verify certificates.

Deployment-wide durable admission bounds worker reservations and reserves
configured hourly headroom for the gateway. Worker activation above its compute
price ceiling is rejected. These controls use operator-verified quotes; they do
not change the account spend cap or guarantee storage/network prices. No numeric
RunPod stock or Pod-count quota is assumed. There is no fleet FIFO/priority
queue; busy workers provide bounded local backpressure.

## Build and configuration

Build from the repository root for the target CPU architecture:

```sh
docker buildx build --platform linux/amd64 --load \
  -f witness-server/worker/Dockerfile.pods -t witness-pods:local .
```

Publish through the operator's release process and configure the immutable image
digest in [deploy/revivor.env.example](deploy/revivor.env.example). Set the actual
deadline, canonical operator hotkey, frozen prompt hashes, rate ceilings, keys and
HTTPS origin. Keep the deployment namespace/master key stable for the round.
Both roles must use the image's frozen pricing bytes. Images must use digests.

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

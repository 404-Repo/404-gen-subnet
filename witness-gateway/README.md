# Witness gateway

A public Python Cloudflare Worker authenticates registered Bittensor miners, queues
RunPod jobs, and connects each miner to its assigned witness worker. A Durable
Object per round and hotkey owns allocation state and the infrastructure allowance.

Both the miner and the RunPod worker open outbound WebSockets to Cloudflare.
The gateway forwards bounded messages between them. The witness performs TLS
verification and writes evidence to R2. Retained responses stream from R2 through
an authenticated download route.

```mermaid
sequenceDiagram
    participant M as Miner
    participant G as Cloudflare gateway
    participant Q as RunPod queue
    participant W as Witness worker
    M->>G: Signed exchange request
    G->>G: Authenticate and reserve allocation
    G->>Q: Enqueue with private connection capability
    Q->>W: Start job when capacity is available
    W->>G: Claim allocation over outbound WebSocket
    G->>G: Save claim and session metadata
    M->>G: Read session and open signed WebSocket
    M->>G: TLS bytes and proofs
    G->>W: Forward messages
    W->>G: TLS bytes and verification acknowledgements
    G->>M: Forward messages
```

## Allocation rules

- Each miner has one current request, including queue waiting.
- Repeating the same request ID returns its existing state. Different parameters
  under that ID are rejected.
- A reservation consumes one job from `MAX_JOBS_PER_MINER` before dispatch.
  Restarts, releases, and replacements do not reset this count.
- The worker capability is private and single-use. Duplicate RunPod deliveries
  and workers arriving after replacement cannot attach.
- `POST /release/<ID>` retires that allocation. It is safe to repeat. A subsequent
  request can queue another worker. `/run` also accepts a signed `replace` ID
  to reserve the replacement in the same operation.
- Closing a live connection retires the allocation. A worker already capturing
  OpenAI continues saving evidence independently of that connection.

A RunPod job may reuse a warm worker. A new allocation does not promise a different
physical machine. Each allocation lasts for one OpenAI exchange.

## Public API

The Python `WitnessClient` handles these routes:

| Route | Purpose |
| --- | --- |
| `POST /run` | Reserve and queue a signed exchange. |
| `GET /status/<ID>` | Read allocation state, deadline, and worker session metadata. |
| `POST /release/<ID>` | Retire this miner's allocation. |
| `GET /exchanges/<ID>/connect` | Upgrade to the miner's live WebSocket. |
| `GET /exchanges/<ID>/progress` | Read recorded witness progress. |
| `GET /exchanges/<ID>/response.sse` | Download a retained response. |

`POST /run` and `GET /status/<ID>` return the same allocation snapshot:
`round`, `hotkey`, `exchange_id`, `queue_state`, `deadline`, and `session`.
The session is `null` until the worker reports its metadata. The client polls
status every five seconds and connects when the allocation is `connected` and
session metadata is present. Retired or finished allocations cannot be connected,
even if their session metadata remains available.

Miner HTTP requests and WebSocket upgrades require a fresh `btauth/1` signature
over method, path, body hash, nonce, hotkey, and receiver `404-witness`. Authentication
is valid for ten seconds. Used nonces are rejected, and ownership is checked for
every route. Clock synchronization is required.

The signed job contains `operation`, `hotkey`, `receiver`, `exchange_id`, `deadline`,
`provider`, and an optional `replace` ID, in that order. `provider` is `api` or
`codex`. The gateway assigns the active round and bounds the deadline by `ROUND_END`.
The worker verifies the signed job and its configured round before claiming.

`GET /workers/<round>/<hotkey>/<ID>` is the worker's WebSocket upgrade route. Its
Bearer capability comes from the private RunPod job. It is never returned to the
miner or stored in public transcripts. The operator's RunPod API key stays in
Cloudflare's encrypted secrets.

## Live transport and recovery

Binary WebSocket messages carry TLS bytes in chunks of at most 256 KiB. Text
messages carry the two verification claims, acknowledgements, and an explicit
end marker. Each TLS direction is limited to 16 MiB. Claims are limited to 2 MiB
each. The gateway also caps total messages and bytes per connection. Socket
attachments retain counters across Durable Object hibernation.

The gateway holds no live transcript buffer. Each endpoint receives messages in
WebSocket order. A broken socket ends that live TLS exchange. The miner can use
`resume()` for saved evidence or `release()` followed by `create()` for a new
attempt. Paid requests are not automatically repeated.

If the RunPod enqueue reply is lost, the allocation remains `uncertain`. A process
interruption during dispatch can leave it `submitting`. Retrying the same ID does
not dispatch again. A worker with the original capability can still attach, or
the miner can release the allocation. Late dispatch replies cannot restore a
retired request. Network calls to RunPod happen outside the state lock.

The witness's request, response, and usage remain its responsibility. Allocation
state uses `submitting`, `queued`, `uncertain`, `connected`, `finished`, and `retired`.
A competition **submission** is separate from an exchange allocation.

## Configuration and deployment

Python runs inside Cloudflare's Python 3.14 Pyodide/workerd runtime. The application
uses the standard library and Workers bindings, with no Python packages to upload.
The configured 30,000 ms CPU limit matches the Workers Paid default. Membership
refreshes require the Paid plan because they exceed the Free plan's 10 ms allowance.
Node remains a build/test dependency for Wrangler and the cryptographic bundle.

[crypto.mjs](src/crypto.mjs) is the complete JavaScript bridge: 33 lines calling
the existing Polkadot SS58, sr25519, and Substrate storage-key functions. The
build statically imports the existing WASM binary. Routing, authentication
policy, registration reads, allocation, alarms, and relay handling are Python.
No additional service or cryptographic implementation is required.

Set [wrangler.jsonc](wrangler.jsonc) for the deployment:

The checked-in configuration uses the fixed round `test-0` and live Finney subnet
17 membership. Its availability is independent of competition phases. RunPod
must use `WITNESS_ROUND=test-0`; signed request deadlines still limit each job.

`TEST_HOTKEY` optionally admits one operator-controlled hotkey without subnet
registration, only in `test-0`. It still requires valid signatures and follows
the same membership freshness, replay, ownership, rate and job limits. Remove or
empty the variable to disable it. Regular miners continue to use the live Finney membership snapshot.

1. Bind private KV as `REGISTRY`, and the witness evidence bucket as `TRANSCRIPTS`.
2. Set `ROUND`, `RUNPOD_ENDPOINT`, `MAX_JOBS_PER_MINER`, and `ROUND_END` in Unix
   seconds. Set the same round as `WITNESS_ROUND` on RunPod. Registration defaults
   to Finney subnet 17 (`NETWORK=finney`, `NETUID=17`). `SUBTENSOR_RPC_URL` is the
   HTTPS endpoint used for chain reads.
3. Store the endpoint-restricted RunPod key with
   `npx wrangler secret put RUNPOD_API_KEY`.
4. Keep the configured one-minute Cron Trigger enabled. The gateway reads
   registration from a finalized Finney block and replaces the private KV
   `membership.json` snapshot. Check for a `membership_refreshed` log before
   opening the round. Refreshes also run while the round is closed.
5. Run `npm run build`, then `npx wrangler deploy --dry-run` before deployment.

The build copies Python sources into `dist/python` and bundles only the crypto
bridge. Wrangler uploads that Python entry point and its modules with
`python_workers`, `python_workers_314`, and `python_dedicated_snapshot` enabled.
The deployment snapshot captures Python imports and initialization so new instances
restore the initialized application. `requirements.txt` and `requirements-dev.txt`
are for local diagnostics and tests; they are not Worker package manifests. The pinned
Wrangler may warn about `requirements.txt`, but does not upload those packages.

Each miner request checks this cached registration list. Chain reads run on the
schedule, independently of miner traffic. Registration changes become visible
after refresh and KV propagation. A failed refresh preserves the previous
snapshot and its timestamps and emits `membership_refresh_failed`. If either
the snapshot or its finalized block is more than five minutes old, the gateway
returns `membership_unavailable` until a fresh snapshot is available.

Control requests have a per-miner burst of 20 and refill at two requests per
second. New allocations have the persistent per-round job allowance. These
limits govern infrastructure usage. Token accounting belongs to validation.

The request deadline bounds total queue and execution time, up to seven days.
RunPod's endpoint execution timeout separately caps time on a worker; the gateway
does not override it. Keep that timeout enabled (40 minutes on the test endpoint).
The worker also limits miner setup before generation. `ROUND_END=0` closes
gateway access. Existing connections close at their assigned deadline. Preserve
the Durable Object namespace and migration history across deployments.

Keep the Worker name, `MinerAdmission` class, `MINERS` binding, and `v1` SQLite
migration. Object names remain `<round>/<hotkey>`. Storage keys `job/<ID>`,
`nonce/<nonce>`, `current`, `count`, and `rate`, job field names, alarms, and socket
attachments retain their existing formats. The Python migration requires no
Durable Object migration entry or data reset.

Deploy the client, gateway, and RunPod image together between generation rounds.
The protocol uses live sockets throughout; public R2 objects are evidence.

## Checks and reading order

Read [auth.py](src/auth.py), [membership.py](src/membership.py), [worker.py](src/worker.py), then
[the exchange sequence](../witness-client/SEQUENCE.md).

Use Node 22.13+ and Python 3.13 in the witness-client test environment. Install
the gateway checking tools there with `python -m pip install -r requirements-dev.txt`.
Application records use typed dictionaries, and [bindings.py](src/bindings.py)
types the Workers APIs used by the application. [runtime.py](src/runtime.py)
contains the explicit Python/JavaScript conversions and the state-lock helper.

```sh
npm ci --ignore-scripts --no-audit --no-fund
PYTHON=/path/to/test-environment/bin/python npm run check
PYTHON=/path/to/test-environment/bin/python npm test
```

Tests use real sr25519 signatures, Cloudflare's local `workerd` runtime, persistent
Durable Objects, chain RPC fixtures, and a RunPod stub. They cover registration
changes, failed refreshes, stale membership, duplicate dispatch, replacement,
restarts, ownership, disconnected workers, live WebSocket framing, and 250
simultaneous miners. The burst test measures local gateway admission, not cloud
throughput.

Migration checks seed existing JavaScript storage records, interrupt dispatch,
exercise deadline alarms, and verify actual hibernation with retained socket
counters. A local HTTPS/WSS test runs the unchanged synchronous `GatewayClient`
and `WorkerChannel`, then exercises `WitnessClient.resume()` against fixture R2
evidence. It sends fixture transport bytes and claims, not an OpenAI request or
a native cryptographic proof. It requires OpenSSL and the built witness client.

The [isolated compatibility probe](python-compat/README.md) tests Python importing
the existing WASM verifier independently of application policy. Wrangler's dry
run checks the upload bundle; it does not verify a cloud deployment. Run the
release checklist's Cloudflare → RunPod → OpenAI validation before opening a
generation round.

Pending membership policy work is separate: a ten-minute refresh cadence,
bounded retries, and the associated freshness tolerance. The migration retains
the one-minute schedule, 45-second refresh limit, and five-minute freshness checks.

[client.py](client.py) is a low-level queue diagnostic. Miner applications use
`WitnessClient`. Deployment validation is tracked in the
[server release checklist](../docs/witness-server-release-plan.md).

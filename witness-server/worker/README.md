# Witness worker

One RunPod Serverless job handles one OpenAI exchange. The worker claims its
allocation through an outbound WebSocket to Cloudflare, then relays the miner's
TLS connection to OpenAI. The existing verifier checks the handshake, credential
proof, request, and response.

The worker saves authorization and the request before forwarding billable bytes.
After that, it captures and saves the response even if the miner or gateway
connection disappears. The saved response precedes delivery to the miner.

Once the worker starts the relay, the miner has 60 seconds to send its ClientHello
and five minutes total to authorize the request. These fixed deadlines include
transfers and proof preparation; sending occasional bytes does not extend them.
Queue waiting does not consume this setup window. RunPod's endpoint execution
timeout caps the entire job, including generation and evidence persistence.

Evidence lives under `rounds/<round>/<hotkey>/<request-id>/`. The validator lists
that round/hotkey prefix to discover request records, including interrupted
attempts. Each exchange writes only its own directory. Allocation state and the
job allowance live in Cloudflare's private Durable Object.

## Reading order

1. [identity.py](identity.py): signed miner identity and private connection capability.
2. [channel.py](channel.py): bounded TLS and proof messages over the WebSocket.
3. [worker.py](worker.py): verification, provider capture, and evidence persistence.
4. [storage.py](storage.py): R2 evidence access.
5. [verify.mjs](verify.mjs): entry point into the TLS/ZK verifier.

## Build and deployment

Build from the repository root:

```sh
docker build --platform linux/amd64 -f witness-server/worker/Dockerfile -t 404-witness-worker:local .
```

The image includes Node, Python, the native verifier, and `verifying.bin`.
Set `WITNESS_ROUND` and the R2 variables read by [storage.py](storage.py).
Use a queue-based RunPod Serverless endpoint. Connections to the gateway and
OpenAI are outbound, so no public worker port is required.

The gateway supplies a single-use connection capability, the miner's signed job,
and the effective round deadline. A rejected capability stops the job before
provider connection or evidence writes. Replacing a request does not interrupt
a previous worker that is already capturing a provider response.

Both providers share the deployment. API jobs use `api.openai.com/v1/responses`;
subscription jobs use `chatgpt.com/backend-api/codex/responses`. This choice also
controls verification. Deploy matching gateway and client versions between rounds.

From this directory, test the built image and Docker exclusions:

```sh
WITNESS_TEST_IMAGE=404-witness-worker:local python -m pytest -q tests/test_container.py
```

See [server checks](../README.md#local-checks) for setup. Local TLS tests use real
WebSockets and native credential proofs with local provider fixtures.

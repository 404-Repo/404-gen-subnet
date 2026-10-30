# Witness worker

The worker runs on an ordinary CPU Pod with a direct authenticated TLS service on
`8000/tcp`. The gateway supplies the miner with its public mapped endpoint,
certificate authority and scoped allocation grant. Outbound gateway connections
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

Content-addressed completed evidence lives under `evidence/v1/`. The Pod returns
expected hashes and object-specific signed R2 URLs through authenticated TLS;
the miner downloads saved bytes directly from R2. Private journals, pending
spools, allocation capabilities and TLS keys remain private. The direct listener
verifies the assigned hotkey and allocation grant, and checks the durable R2
selection fence before capturing and authorizing new calls.

## Reading order

1. [pod.py](pod.py): worker activation, capture ownership, persistence retries and drain.
2. [identity.py](identity.py): canonical miner identity and round-scoped paths.
3. [channel.py](channel.py): bounded TLS and proof messages over the WebSocket.
4. [worker.py](worker.py): verification, provider capture and evidence persistence.
5. [storage.py](storage.py): R2 evidence access.
6. [verify.mjs](verify.mjs): entrypoint into the TLS/ZK verifier.

## Build and deployment

Build from the repository root:

```sh
docker build --platform linux/amd64 -f witness-server/worker/Dockerfile.pods -t 404-witness-pods:local .
```

The image includes the worker, Node, the native verifier, `verifying.bin`,
Chromium, the renderer and the Python dependencies for embeddings and result
saving. The `worker` role is started with `python -m pod_gateway worker` and is
the default. Set `WITNESS_IMAGE` to this image's immutable digest. Build target
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

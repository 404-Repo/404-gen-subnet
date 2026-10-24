# Witness worker

The worker runs on an ordinary CPU Pod and connects outbound to the Python
gateway. It relays the miner's TLS connection to OpenAI. The verifier checks the
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

The image includes the Python gateway and worker, Node, the native verifier and
`verifying.bin`. The `worker` role is started with `python -m pod_gateway worker`.
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

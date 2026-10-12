# Witness server

The [witness worker](worker/README.md) is a RunPod queue handler that captures
one OpenAI exchange, verifies it, and saves its request, response, and usage in R2.

The [witness gateway](../witness-gateway/README.md) runs on Cloudflare. It
authenticates registered Bittensor hotkeys, allocates RunPod jobs, and forwards
live WebSocket messages.

The [verifier](verifier/README.md) runs inside the worker. It is shared with the
local test fixtures. Its native proof binary
is built from the single source in `witness-client/native`.

The public client is [`WitnessClient`](../witness-client/README.md).
Transcripts are grouped under `rounds/<round>/<hotkey>/`; request IDs are internal
transport and recovery handles. Recovery does not start another generation.

See the [release checklist](../docs/witness-server-release-plan.md) for deployment
preparation and validation.

## Local checks

Use Node 22 and Python 3.13 for gateway development. Build the client/native verifier and install
its development dependencies as described in
[client development](../witness-client/DEVELOPMENT.md). The matching proof
parameters are included in `witness-server/verifier/params/`.

From the repository root:

```sh
npm --prefix witness-gateway ci --ignore-scripts
npm --prefix witness-server/verifier ci --ignore-scripts
npm --prefix witness-server/verifier run build:proof
python -m pip install -r witness-server/worker/worker-requirements.txt
python -m pip install -r witness-gateway/requirements-dev.txt
```

Run each suite from its directory:

| Directory | Checks |
| --- | --- |
| `witness-gateway` | `npm test`, `npm run check` |
| `witness-server/worker` | `python -m pytest -q`, `python -m mypy` |
| `witness-server/verifier` | `npm test`, `npm run test:proof` |
| `witness-client` | `python -m pytest -q`, `python -m mypy` |

Worker tests use real WebSockets, provider TLS, and native credential proofs.
Storage uses an in-memory fixture. Gateway tests run in Cloudflare's local runtime.

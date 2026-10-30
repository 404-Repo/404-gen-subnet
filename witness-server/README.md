# Witness server

The [Python gateway](../witness-gateway/README.md) authenticates registered
Bittensor hotkeys and assigns ordinary RunPod CPU Pods. The
[witness worker](worker/README.md) captures OpenAI exchanges, verifies them and
saves requests, responses, proofs and usage in private R2 storage.

The [verifier](verifier/README.md) runs inside the worker and in local fixtures.
Its native proof binary is built from `witness-client/native`. The public client
is [WitnessClient](../witness-client/README.md). Recordings preserve call identity,
output bytes and delivery order so recovery and replay can use saved responses.

The [Pod guide](../witness-gateway/PODS.md) describes the lifecycle and runtime
pins. Each manifest records its exact image, bootstrap, limits and verification
scope. Use the complete manifest when reproducing a tested version.
See the [release checklist](../docs/witness-server-release-plan.md) for integration
and deployment gates.

## Local checks

Use Node 22 for the verifier. The Pod runtime pins Python 3.14.9. Build the
client/native verifier and install development dependencies as described in
[client development](../witness-client/DEVELOPMENT.md). Matching proof parameters
are included in `witness-server/verifier/params/`.

From the repository root:

```sh
python -m pip install -c witness-gateway/constraints-pods.txt -r witness-gateway/requirements-pods.txt
npm --prefix witness-server/verifier ci --ignore-scripts
npm --prefix witness-server/verifier run build:proof
```

Run each suite from its directory:

| Directory | Checks |
| --- | --- |
| `witness-gateway` | `python -m mypy`, `PYTHONPATH=../witness-client/src:../witness-server/worker python -m pytest -q` |
| `witness-server/worker` | `python -m pytest -q`, `python -m mypy` |
| `witness-server/verifier` | `npm test`, `npm run test:proof` |
| `witness-client` | `python -m pytest -q`, `python -m mypy` |

Worker tests use real local WebSockets, provider TLS and native credential proofs.
Gateway and storage tests use fixtures. These checks do not provision resources
or make paid provider requests.

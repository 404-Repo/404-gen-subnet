# Witness gateway

The Python gateway authenticates Bittensor miners, assigns ordinary RunPod CPU
Pods and forwards bounded WebSocket connections. Workers capture and verify
OpenAI exchanges, then save evidence and usage in private R2 storage. A separate
revivor restores the gateway and drains resources owned by the deployment.

See [PODS.md](PODS.md) for configuration, lifecycle, recovery, budgets, replay and
the immutable runtime manifests. The image is built from
[Dockerfile.pods](../witness-server/worker/Dockerfile.pods). Its entrypoint supports
`gateway`, `worker` and `revivor` roles through `python -m pod_gateway`.

The [client](../witness-client/README.md) binds requests to a frozen generation.
[The worker](../witness-server/worker/README.md) saves authorization before
releasing provider requests and persists responses before delivery. R2 contains
the durable inference claims and recordings; RunPod remains the source of truth
for Pod identity and lifecycle.

## Local checks

Install the client development tools and native proof library as described in
[client development](../witness-client/DEVELOPMENT.md), then install
`requirements-pods.txt` with `constraints-pods.txt`. From this directory:

```sh
python -m pip install -c constraints-pods.txt -r requirements-pods.txt
python -m mypy
PYTHONPATH=../witness-client/src:../witness-server/worker python -m pytest -q
```

The tests use local provider, RunPod and storage fixtures. They do not provision
Pods or send paid inference requests. Deployment acceptance uses the exact image,
client, proof parameters and configuration in the
[release checklist](../docs/witness-server-release-plan.md).

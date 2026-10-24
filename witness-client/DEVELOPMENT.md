# Witness client development

The [sequence diagrams](SEQUENCE.md) trace generation reservation, TLS setup,
credential disclosure and response capture. See [TIMINGS.md](TIMINGS.md) for
monotonic diagnostics and the [Pod guide](../witness-gateway/PODS.md) for lifecycle,
recording and recovery contracts.

A generation binds frozen prompt bytes and a seed.
Each invocation records its start and delivery positions. An exchange captures
one provider request and response; identical requests can share that exchange.

Start with these files:

1. [client.py](src/witness_client/client.py): generation-bound requests, proofs and recovery.
2. [calls.py](src/witness_client/calls.py): generation identity and request normalization.
3. [pods.py](src/witness_client/pods.py): Pod reservation, dispatch, snapshots and recorded delivery.
4. [gateway.py](src/witness_client/gateway.py) and [tunnel.py](src/witness_client/tunnel.py): signed HTTP access and bounded WebSockets.
5. [request.py](src/witness_client/request.py): provider request preparation and the credential boundary.
6. [delivery.py](src/witness_client/delivery.py): bounded HTTP reads and transient retries.
7. [transport.py](src/witness_client/transport.py): TLS record isolation and disclosure.
8. [proof.py](src/witness_client/proof.py) and [native/](native/): the native credential proof.
9. [runtime.py](src/witness_client/runtime.py) and [replay.py](src/witness_client/replay.py): trusted runner integration and recorded replay.

## Client reference

### Settings and credentials

Pin `round`, `witness_url` and matching `proof_parameters` in `WitnessConfig`.
`pods-v1` is the default transport. The trusted runner binds the generation
before passing a client to miner code. The client verifies session identity,
provider route and proof-key hash before sending credentials. HTTPS and provider
TLS use the system trust store.

Pass an already loaded sr25519 keypair as `hotkey=...`. Set `codex_home` or `codex`
to select a local subscription login directory or executable. API keys must
contain 64–4096 characters from `A-Z`, `a-z`, `0-9`, or `._~+/=-`. Subscription
refresh requires at least five minutes of remaining token validity.

Generation-bound `create()` uses OpenAI's `ResponseCreateParamsBase` through
`Unpack`, with streaming enabled internally, and returns a complete typed `result.response` plus
`output_text` and `usage`. Background generation is rejected. Subscription
requests use `store=false`. Serialized request bodies are limited to 8 MiB.

The package includes full type annotations and `py.typed`. Public types include
`WitnessConfig`, `WitnessResult` and `Usage`.

### Recovery outcomes

`WitnessError.exchange_id` identifies an attempt for recovery or inspection.
`source` identifies `client`, `gateway` or `provider`; `status_code` and `code`
retain service failures. Provider errors can retain `provider_code`, `param`,
`provider_message` and the typed `error.response` with recorded usage.

`resume()` requires the saved invocation in the original state directory. It
polls the same exchange and returns its durable result. Repeating a generation
consumes its existing recorded prefix and preserves delivery order. A pending
capture stays pending; delivery recovery does not start another inference.
`release()` retires a stuck capture without resetting usage or erasing its data.

### Timeouts and retention

Each delivery phase retries temporary failures for up to `delivery_timeout`
seconds (default 600), with a 30-second timeout per HTTP attempt. The setting must
be finite and positive. It does not extend the operator's generation deadline or
evidence retention. Preserve local recovery state and unresolved accounting.

## Build and verification

The distribution name is `404-witness-client`; the Python import remains
`witness_client`. Wheels use the filename prefix `404_witness_client-`.

Artifact hashes, validation results, and the exact source tag and commit are
recorded in [release.json](release.json) for the last verified release. That
record does not describe subsequent source changes. When using a release wheel,
check its SHA-256 against the record and use the checkout of its source tag.
For the current checkout, follow the [README installation steps](README.md#install).
Proof parameter sizes and hashes are pinned in
[native/parameters.go](native/parameters.go). The verification-key SHA-256 is
`6478059e4ad0d39e8c157c76cea9c6dd777c2ab5143cf8694876094facf39b16`.

Build releases from a clean `git archive` of an annotated source tag. Record the
tag, commit, artifact hashes, and verification results in [release.json](release.json).
Test and publish the same wheel. Each source change requires a new package version;
released artifacts and tags are immutable.

Maintainers build a wheel on the target OS/architecture, with Go 1.27.1, a C
compiler, Python 3.11+, `build==1.6.1`, `setuptools==84.0.0`, and `wheel==0.48.0`:

```sh
cd witness-client
python build_wheel.py
```

Both native Docker builds pin the Go 1.27.1 image by digest and use
`GOTOOLCHAIN=local` to prevent an implicit compiler switch. The module requires
Go 1.27.1 or newer. Record `go version -m` for the actual shared library and CLI
in each release manifest, and check Go's [supported release lines](https://go.dev/doc/devel/release#policy)
before the next release. Updating the compiler does not require new proof parameters.

For the Linux target (from the repository root):

```sh
docker build --platform linux/amd64 -f witness-client/Dockerfile -t 404-witness-python .
docker run --rm --platform linux/amd64 -v "$PWD/witness-server/verifier/params:/app/witness-server/verifier/params:ro" 404-witness-python
```

The Docker build runs `auditwheel repair` and checks the resulting
`manylinux_2_34_x86_64` wheel before installing it. A native `linux_x86_64`
development wheel is not a PyPI release artifact.
The release wheel is under `/app/witness-client/dist` in that image. Copy it out
with `docker cp` for distribution.

## Licence packaging

`LICENSE` is an unchanged copy of the repository's MIT licence.
`THIRD_PARTY_NOTICES.md` preserves the licences, patent grants, and attributions
for the linked Go modules and runtime, including gnark's nested MIT reflectwalk
code. The combined SPDX expression in `pyproject.toml` describes the distribution,
including its bundled native code. It does not change the client's MIT licence.
Both documents are explicitly included through `project.license-files` and allowed
through the Docker build context. Setuptools installs them under
`404_witness_client-<version>.dist-info/licenses/`.

When updating Go or its modules, compare `go version -m` on the resulting library
with the notice inventory. Inspect the compiled packages from `go list -deps -json`
for nested licences and embedded notices as well as module-root files. Update the
inventory and copy the upstream text without rewriting its terms or copyrights.
Before release, inspect the actual wheel's `License-Expression`, `License-File`
entries, and the referenced files; verify their bytes against the source documents.
Python dependencies are separate distributions and supply their own licence files.

## Dependency security

Runtime and development requirements use lower bounds and compatibility limits.
The client uses internal interfaces from tlslite 0.8; widening that range
requires integration tests. Docker pins build and test tools for reproducibility.

Audit the installed release environment with `pip-audit --strict` and record
resolved versions and findings alongside the artifact. Audit reports are retained
in [results/](results/).

## Tests

Client and Pod gateway checks cover delivery retries, credential refresh, recovery without
another generation, certificate validation, and proof parameter integrity.
The worker's TLS integration tests carry large requests and responses through
WebSockets with real proofs. They check authorization before generation,
provider rejections, usage recording, and capture after miner disconnection.

Native ABI 2 verifies parameter sizes and hashes before deserialization.
`tests/test_proof.py` and `native/python_parameters_test.go` cover altered,
mismatched, missing, and replaced parameter files. Run native tests with
`go test ./...` from `native/`, using the parameters included in the repository.

Install the development tools with `pip install -e '.[dev]'` after building the
native library. From this directory, run:

```sh
python -m mypy
GOMAXPROCS=2 GOMEMLIMIT=768MiB python -m pytest -q
```

Strict type checking covers the package, tests and build scripts.
The wheel includes `py.typed`; request settings use the OpenAI SDK's field types.

For worker integration tests, install Node and the Pod dependencies,
then build the verifier. From the repository root:

```sh
python -m pip install -c witness-gateway/constraints-pods.txt -r witness-gateway/requirements-pods.txt
npm --prefix witness-server/verifier ci --ignore-scripts
npm --prefix witness-server/verifier run build:proof
cd witness-server/worker
GOMAXPROCS=2 GOMEMLIMIT=768MiB python -m pytest -q tests/test_tls.py
```

The matching proof parameters are included at `witness-server/verifier/params`.
Integration fixtures use local TLS servers, dummy OpenAI credentials, ephemeral
Bittensor keys, and real credential proofs. Fixtures supply their certificate
trust and provider addresses internally. Miners can test their code locally
with the normal configuration pointing to the deployed witness gateway.

# Witness client development

The [sequence diagrams](SEQUENCE.md) trace call reservation, TLS setup,
credential disclosure and response capture. See [TIMINGS.md](TIMINGS.md) for
monotonic diagnostics and the [Pod guide](../witness-gateway/PODS.md) for lifecycle,
recording and recovery contracts.

Logical requests are allocation-scoped and independent of execution inputs.
`calls.py` defines canonical identities, `client.py`/`pods.py` handle capture and
recovery. The separate [miner runtime](../miner-runtime/) owns execution:
`miner_runtime.runner` invokes the submitted function, `miner_runtime.observations`
records its model-call boundary, and `miner_runtime.validator` consumes a verified
bundle without an inference client. See the [full contract](../witness-gateway/PODS.md#execution-and-replay-contract).

## Client reference

Pin `round`, `witness_url` and matching `proof_parameters` in `WitnessConfig`.
Allocation/reconnect checks the round once; model requests carry no round field.
Pass the caller's loaded sr25519 `hotkey`. The private key stays local.
`openai_api_key` and `subscription="codex"` are mutually exclusive. `codex_home`
and `codex` select subscription credential storage/refresh. Provider TLS uses
system certificate verification; direct Pod access uses the authenticated
allocation's certificate, address, server name and expiring scoped grant.

`responses.create()` accepts `ResponseCreateParamsBase` model arguments and returns
an SDK `Response` by default or buffered typed events for `stream=True`. Provider
HTTP errors use SDK exceptions; failed/incomplete Responses remain objects.
`request_id` selects fresh capture identity, and optional subscription `session_id`
is a verified routing header. Background generation is rejected. Subscription
normalization forces `store=false`. Request bodies are bounded to 8 MiB.

`WitnessError.exchange_id` identifies an attempt for recovery. `resume()` polls
that exchange using saved local job state and retrieves its durable Response.
`release()` can retire an unclaimed capture; it cannot repeat inference or erase
uncertain usage. `delivery_timeout` is a positive per-phase retry window (default
600 seconds), independent of the server's access/capture deadlines and retention.
Keep local state private/persistent. No SDK inference retry is enabled.

Trusted hosts use `capture_context`, `capture_response(key, body)` and
`saved_response(exchange_id)` for capture and recovery. The immutable
`CaptureContext` supplies the provider, state directory, delivery timeout and a
local namespace derived from the gateway origin, hotkey, round and provider.
`capture_response` returns a `CapturedResponse` with the exchange ID, HTTP status
and retained response bytes; an existing logical call follows the same recovery
path as `responses.create()`. `saved_response` only reads retained local bytes.
`miner_runtime.runner.CaptureSource` defines this interface for trusted hosts; the
runtime owns execution paths, locking and observations.

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

Maintainers build a wheel on the target OS/architecture, with Go 1.27.3, a C
compiler, Python 3.11+, `build==1.6.1`, `setuptools==84.0.0`, and `wheel==0.48.0`:

```sh
cd witness-client
python build_wheel.py
```

Both native Docker builds pin the Go 1.27.3 image by digest and use
`GOTOOLCHAIN=local` to prevent an implicit compiler switch. The module requires
Go 1.27.3 or newer. Record `go version -m` for the actual shared library and CLI
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

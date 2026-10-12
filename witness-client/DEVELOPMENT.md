# Witness client development

The [sequence diagrams](SEQUENCE.md) trace job queueing, TLS setup, both
disclosures, and response capture across the client and witness services.

A **submission** is a miner's entry for a competition round. A **job** is one
queued witness worker run. An **exchange** is the OpenAI request and response
recorded by that worker. Proofs and encrypted TLS bytes travel over a live WebSocket.

Start with these files:

1. [client.py](src/witness_client/client.py): the public `create()` / `resume()` / `release()` flow and early disclosure.
2. [request.py](src/witness_client/request.py): OpenAI request preparation, subscription settings, and the credential boundary.
3. [gateway.py](src/witness_client/gateway.py) and [tunnel.py](src/witness_client/tunnel.py): hotkey-signed allocation, live WebSocket messages, and recovery.
4. [delivery.py](src/witness_client/delivery.py): bounded HTTP reads and transient retries.
5. [recovery.py](src/witness_client/recovery.py): protocol and local authorization checks for recovery.
6. [transport.py](src/witness_client/transport.py): TLS record isolation and early disclosure.
7. [proof.py](src/witness_client/proof.py) and [native/](native/): the narrow C ABI and credential proof.

`create()` serializes and validates the OpenAI request body once and checks
credentials before joining the queue. After a worker is ready, it verifies and
saves the session, reloads credentials, and builds the authenticated request
using the same body. It then sends the request and retrieves the verified
response. `resume()` retrieves an authorized exchange from saved state.

The client uses the witness gateway for job queueing, disclosure, and recovery.
Each signed job includes `provider: "api"` for an API key or `provider: "codex"`
for a subscription. The queue signature covers the fields in order:
`operation`, `hotkey`, `receiver`, `exchange_id`, `deadline`, `provider`,
then an optional `replace` ID.
The gateway adds its configured round to the RunPod job after authenticating
the miner. The worker checks that assignment against its configured round.
HTTP retries keep the same exchange ID and use a 30-second timeout per attempt.
Encrypted traffic uses bounded WebSocket messages. TLS buffers are flushed before
each disclosure callback. Broken live streams use result recovery or an explicit
new allocation.

## Client reference

### Settings and credentials

The client checks that the session matches the requested provider and proof-key
hash before sending OpenAI credentials. Witness URLs require HTTPS. The client
uses the system trust store to verify both witness and provider certificates.

Pass your miner's already loaded sr25519 keypair as the required `hotkey=...`
argument. Your miner code manages wallet loading and unlocking. Set
`codex_home` or `codex` in configuration to use a different subscription login
directory or Codex executable.

API keys must contain 64–4096 characters from `A-Z`, `a-z`, `0-9`, or
`._~+/=-`. Subscription credentials are refreshed after the worker is ready
when needed, requiring at least five minutes of remaining token validity.

`input` uses OpenAI's `ResponseInputParam` type (or a plain string). Additional
keyword arguments use the SDK's `ResponseCreateParamsStreaming` through `Unpack`,
providing editor completion and static type checking. They are forwarded into
the OpenAI request body. Pass `stream=True` to `create()`, which returns
the full typed OpenAI response in `result.response`, with `output_text` and
`usage` shortcuts. Background generation and requests without `stream=True` are
rejected before queueing. Subscription requests set `store=false` and default
`instructions` to an empty string. The serialized request body is limited to 8 MiB.

The package includes full type annotations and `py.typed`. Public types include
`WitnessConfig`, `WitnessResult`, and `Usage`.

### Recovery outcomes

`WitnessError.exchange_id` identifies an attempt for recovery or operator
inspection. `source` identifies `client`, `gateway`, or `provider`;
`status_code` is the HTTP status from that service. `code` identifies the failure.
OpenAI errors retain `provider_code`, `param`, and `provider_message` when supplied.
Incomplete or failed generations retain their typed response and usage in
`error.response`. Both `create()` and `resume()` raise `WitnessError` for exchange
failures; invalid local request arguments raise `ValueError` or `TypeError`.
Recovery returns the same retained outcome; generation retries are explicit new calls.

| Error code                | Meaning                                    |
| ------------------------- | ------------------------------------------ |
| `queue_incomplete`        | A worker may be queued; keep the ID.       |
| `exchange_not_authorized` | Authorization was not prepared.            |
| `recovery_unavailable`    | Required local recovery files are missing. |
| `unsupported_protocol`    | Saved state uses an unsupported protocol.  |
| `invalid_gateway_response` | Gateway session or status metadata is malformed. |

Completed and failed workers release their current allocation. If a gateway or
worker interruption leaves an allocation stuck, `client.release(exchange_id)`
retires it. The next `create()` consumes another job from the existing allowance.
Releasing does not cancel a previous generation already being captured.

Usage, provider rejections, and uncertain outcomes stay in each request's evidence
for validation. They do not become permanent allocation locks.

### Timeouts and retention

Each delivery phase retries temporary failures for up to `delivery_timeout`
seconds (default: 600), with a 30-second timeout per HTTP attempt. The delivery
timeout must be finite and greater than zero.

Queue waiting and exchange execution share the generation round's closing time,
within RunPod's seven-day maximum. Credentials are refreshed after waiting.
Gateway access, including recovery, closes when the generation phase ends.
The witness records a seven-day evidence retention timestamp for completed results.

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

For a live queue check, [queue_live_test.py](queue_live_test.py) uses the installed
package and only miner credentials. Run it with `--config witness.json`,
`--hotkey-mnemonic-file /private/test-hotkey.txt`, and `--output /private/test-run`.
Use a disposable test hotkey admitted by the operator. `--interrupt` deliberately
disconnects after request authorization; a fresh invocation with `--recover`
retrieves the retained answer without calling `create()` again. A normal run
spends one small subscription generation; recovery does not start another.

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

Client checks cover queue retries, credential refresh, recovery without
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

Strict type checking covers the package, examples, tests, and build scripts.
The wheel includes `py.typed`; request settings use the OpenAI SDK's field types.

For the queue worker integration tests, install Node and the worker dependencies,
then build the verifier. From the repository root:

```sh
python -m pip install -r witness-server/worker/worker-requirements.txt
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

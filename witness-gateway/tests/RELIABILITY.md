# Call recording and replay investigation

The tested production snapshot is
`e6d29b25a1a4a91df2deab28098a99dc7ff9d5e5`, with test additions in `206bb7ad`.
Calls register through `POST /v1/calls`, carry proofs and TLS bytes through
`GET /v1/exchanges/{exchange_id}/connect`, and recover through
`GET /v1/calls/{exchange_id}`. The miner continues to use `responses.create()`.

The offline results are **462 passed and two strict expected failures**:
254 SDK cases, 149 recording/storage/recovery/replay cases, 50 HTTP service and
reference-miner batch cases, and nine native TLS outcome cases. Live checks below
are additional evidence, separate from that count.

## Reproduction

Use the normal project dependencies and matching native proof artifacts.
The gateway fixture forbids external Python socket connections; native container
runs additionally use `--network none`. Do not substitute an old ignored
`privateauth` binary: missing certificate-time output can make a valid capture
impossible to export.

```sh
PYTHONPATH=shared:witness-client/src:miner-runtime/src:renderer-client/src:witness-gateway:witness-gateway/tests:witness-server/worker \
  python -m pytest -q witness-gateway/tests/test_call_reliability.py \
  witness-gateway/tests/test_provider_outcomes.py witness-gateway/tests/test_replay.py

PYTHONPATH=shared:witness-client/src:miner-runtime/src:renderer-client/src:witness-gateway:witness-gateway/tests:witness-server/worker \
  python -m pytest -q witness-gateway/tests/test_native_outcomes.py

PYTHONPATH=witness-client/src python -m pytest -q \
  witness-client/tests/test_response.py --runxfail -k truncated_sse
```

The last command intentionally exposes two unresolved failures. Both regressions
are strict expected failures during an ordinary suite run; a fix must make them
pass and remove those marks.

The opt-in real-R2 harness uses the configured **existing** `R2_BUCKET`,
`R2_ENDPOINT`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`:

```sh
PYTHONPATH=witness-client/src:witness-gateway python \
  witness-gateway/tests/live_r2_recovery.py --output /private/path/r2-results.json
```

It creates synthetic objects under a fresh `reliability-tests/r2-recovery-<uuid>/`
prefix. Its local report indexes all retained objects. It creates no buckets,
changes no bucket permissions, and makes no model or Pod requests. Credentials
never enter the synthetic objects or report. Import and `--help` perform no writes.

## Unresolved caller-visible bug

An HTTP 200 body containing `response.created` and an output-text delta, followed
by EOF without a terminal SSE event, has inconsistent failure behavior:

- Nonstreaming raises raw `ValueError("Missing completed OpenAI response")`.
- Streaming yields the partial events and ends successfully with `StopIteration`.

The expected public behavior is an SDK error for the incomplete response. A miner
catching SDK errors can otherwise take the wrong branch or use partial output as
complete. The tests exercise the actual response decoder with controlled bytes;
this malformed terminal-SSE case was not reproduced against a live provider.
Production code is unchanged by this testing branch.

## Evidence matrix

| Boundary | Exercised evidence | Result / remaining limit |
|---|---|---|
| Logical identity | Existing recording/replay tests; repeated same-key create/stream observations; 32 independent clients after allocation | Separate occurrences replay; one retained call is charged once. A live duplicate subscription caller recovered the same provider response ID. |
| Safe retry and concurrency | Existing stale-attempt/claim tests; 16 concurrent real-R2 claimants and 16 real-R2 safe retries | One claim and one replacement; delayed old registrations do not replace the newer attempt. These R2 checks count permissions, not actual provider executions. |
| Authorization writes | Existing rejected/uncertain reservation fault tests; real conditional PUTs with injected acknowledgement/readback loss | Exact readback can confirm a committed write. Missing/different/unavailable readback stays uncertain and cannot justify provider release. |
| Process death | Existing SIGKILL matrix, nine boundaries with and without the local spool | No repeated possible dispatch. A full spool or durable manifest can recover completion; lost pre-manifest response bytes can remain irrecoverable pending work. |
| Required evidence | Missing eight objects; twelve altered bindings; native export with seven missing/corrupt required inputs | Missing evidence cannot settle the call; mismatched bindings fail. Native export rejects missing/corrupt request, response, manifest, client/server/authorization ciphertext and claim. |
| Caller delivery | Recorded failure then success on the same key; live lost HTTP registration reply and dropped authorized capture socket | Each occurrence retains its original observation. The live SDK recovered completion internally, and a fresh process resumed the same result. |
| Provider values | Native API-mode TLS fixtures: structured output, empty output, refusal, failed/incomplete Responses, HTTP 429, SSE error | Original text/status/output/events/errors/usage survive capture and actual exporter/validator helpers. Each fixture sends two equal logical calls and observes one provider POST. |
| Incomplete outcomes | Native abrupt HTTP/TLS EOF; HTTP-complete but terminal-SSE-missing decoder regressions | Rejected framing must retain uncertainty and forbid another paid request. Missing terminal SSE currently exposes the bug above. |
| Streaming delivery | Native provider sends its first SSE event and withholds completion | The supplied API's stream must remain unavailable until completion; this is a buffered event iterator. Provider token timings are not reproduced. |
| Replay and accounting | `run_generation` → `export_bundle` with native re-verification → `ReplayWitness` / `run_replay`, external network disabled | Values, typed event order, repeated occurrences, errors and final output match. Incomplete consumption fails. HTTP/SSE errors with missing usage replay their errors but fail budget acceptance. |
| HTTP registration and lifecycle | Service tests cover lost registration replies, duplicate clients, stale setup recovery, selected-Pod checks, authorization, capacity and completed retrieval after round end | These use real local HTTPS/WebSockets with simulated provider/proof. The live run covers signed HTTP registration/polling and capture loss; it does not establish every lifecycle, malformed-message or fragmentation boundary. |

The execution helpers are the repository's development host/validator boundary.
They do not constitute a production sandbox or complete competition validation job.

## Live miner recovery and replay

A new, isolated four-vCPU Pod exercised the actual Gateway, PodRuntime, native
verifier, public mapped TCP/TLS ports, and existing R2 bucket under a fresh prefix.
It used a synthetic miner, an explicit synthetic membership snapshot and a fixed
allocator for that one Pod. It did not exercise chain membership, automatic Pod
provisioning, or a complete live reference-miner render/save batch.

The successful subscription call used a synthetic `session_id` and minimal text.
The test discarded the first committed HTTP registration reply, started an
independent duplicate client during capture, and closed the first capture socket
after durable authorization. Both clients obtained the same completed response.
The retained timing evidence contains one provider-request release for this call.
Nonstreaming output and fourteen typed stream events preserved the response ID,
text and usage: 31 input tokens, nine output tokens, 40 total. A separate process
then resumed the same exchange using retained state.

The API credential returned a quota error. Its recorded failure was reused in
both public API modes without another release. Both live captures passed native
re-verification during export. In a fresh container with networking disabled,
the subscription execution reproduced its output, events, usage and finished
consumption checks. The API failure reproduced its observations, while missing
usage correctly prevented budget acceptance.

Fifty live-test R2 objects, totaling 144,781 bytes, were indexed and retained with
private local copies. Scanning their raw bytes found none of the actual API or
subscription credentials, R2 credentials, control token, miner mnemonic or gateway
private key. This check supplements the protocol's native credential proof.
The temporary Pod was terminated after retaining evidence. Operational IDs,
object keys, secrets and technical logs remain outside Git.

The source candidate built successfully with its pinned production Dockerfile,
but registry upload required renewed Google authentication. The live run therefore
used the existing production-derived image
`sha256:3de431284221779ed95c603ef2bd893f4f715b23702016be9b8bc9cf7107c362`,
with committed service sources and the matching native verifier uploaded privately.
The local report retains source-file hashes and harness differences. This verifies
that code and connection/storage path; it does not verify pulling the new candidate
image from the registry.

## Runtime and result provenance

Native component checks use the existing offline image resolved to
`sha256:251a36a54256f2830237f3e723c361f686fdb01215f5b282d78e52ad2dc442f7`,
with committed Python/JavaScript sources copied into a disposable directory.
Python is 3.14.9, aiohttp 3.14.4 and the OpenAI SDK 3.35.1. The image's original
Go 1.27.1 artifacts are replaced for the native outcome matrix with fingerprinted
Go 1.27.3 artifacts from the current production/client build images. The native
implementation sources match this component snapshot. Native artifact fingerprints:

| Artifact | SHA256 |
|---|---|
| Linux verifier CLI | `64f59ce9a69170ea210f195ee86f3d80968fd91f369f0fae8e1e85c13d0a764e` |
| Linux proof shared library | `17d5473f4f2e1581c088124672bd26bbd4dc02ca48bbf89af687164b80b1e9cb` |
| Verifying key | `6478059e4ad0d39e8c157c76cea9c6dd777c2ab5143cf8694876094facf39b16` |

Verified component commands and counts are retained in the private local run
report. The live worker image has the same Python/aiohttp/SDK versions; its miner
caller used Python 3.13.0 and OpenAI SDK 3.31.0 with a Go 1.27.3 macOS proof library.
Real-R2 object indexes and operational details stay outside Git. No passing suite
establishes exhaustive correctness, live Pod-replacement acceptance, or recovery
of response bytes lost before both spool and durable evidence are available.

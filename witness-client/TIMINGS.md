# Witness performance diagnostics

Set `WITNESS_TIMINGS=1` before starting the client to emit one JSON line per
completed phase to stderr. Client timings are disabled by default. The worker
image enables the same diagnostics, and the gateway writes structured I/O
timings to its existing Workers logs. There is no telemetry service or new
evidence upload.

Each Python record has `event=witness_timing`, `schema=1`, `host`, `trace_id`,
`exchange_id`, `phase`, `start_ms`, `duration_ms`, and an outcome. Only the
exception type is recorded on failure. No arguments, prompts, provider bodies,
headers, proof claims, private keys, credential values or filesystem paths are
included. A failing diagnostic destination is ignored. Timings do not authorize
a request and are never used as budget, verification, deadline or recovery
evidence.

`duration_ms` uses a monotonic clock. Python `start_ms` is relative to its
enclosing operation's trace; Node offsets are relative to that subprocess's
start. Native C ABI/Go durations have `start_ms=null`, with their measured
substeps in `steps_ms` or individual verifier records. Never subtract offsets
between traces or hosts. Parent spans include their children, and client waits
overlap worker operations. Do not add them together as independent work.

Cloudflare's `performance.now()` advances only after I/O in production, so
gateway records use `clock=cloudflare_io`. They measure admission/dispatch I/O,
not precise CPU time. See the [runtime timer documentation](https://developers.cloudflare.com/workers/runtime-apis/performance/).

| Boundary | Measurements |
| --- | --- |
| Client process setup | `initialize`, `prover_initialize`, `native_library_load`, `native_initialize_call`, `http_initialize` |
| Pinned native parameter cache | `parameter_initialize` / `proof_generate` include `parameters_cached`; native steps separate authenticated file read/hash checks, circuit deserialization, key deserialization and mutex wait |
| Credentials | `credentials_before_queue`, `prepare_request_and_credentials`, `subscription_credentials`, `credential_load`, conditional `credential_refresh` |
| Admission and discovery | client `allocation`, `gateway_admission_dispatch`, each `allocation_poll` and `allocation_poll_sleep`; gateway `authenticate`, `admission`, `runpod_dispatch`, `dispatch_persistence` |
| Worker startup to session | `job`, `authenticate_job`, `verifying_key_hash`, `r2_initialize`, `gateway_websocket_connect`, `session_persistence_and_ready`, `client_hello_wait` |
| TLS and fixed probe | `websocket_connect`, `tls_handshake`, `certificate_validation`, `handshake_verification_wait`, `probe_response_wait`; worker `handshake_capture_wait`, `verify_hello`, `verify_handshake`, `handshake_persistence` |
| Local fresh proof | `credential_proof`, `native_prove_call`, native `proof_generate` substep; these exclude TLS and network waits |
| Worker authorization | `authorization_claim_wait`, `verify_authorize`, `verifier_subprocess`, Node `process_start_to_ready` and `verification`, native certificate/verify subprocesses, `native_verifying_key_load`, `native_proof_verify`, `authorization_persistence` |
| Provider | `provider_request_release`, `provider_first_ciphertext_wait`, `provider_response_capture` |
| Completion and delivery | `final_transcript_persistence`, `verify_finish`, `evidence_persistence`, `manifest_persistence`, `final_status_persistence`, `delivery`, client `response_delivery_wait_and_decrypt`, conditional `delivery_status_poll`, `parse_and_save_response`, `evidence_retrieval_wait`, `resume`, conditional `recorded_response_download` |

The worker returns completed diagnostic spans under `timings` in its RunPod job
output. The final `job` span is also logged after the handler exits. Actual
RunPod queue and execution durations come from that job's `delayTime` and
`executionTime`, in milliseconds, when available. They must not be reconstructed
by subtracting timestamps from different computers. Queue delay also includes
any cold-start scheduling/container readiness that RunPod has not partitioned.

The provider first-byte measurement is the first TLS ciphertext received after
request release, which can include a session ticket; it is not first-token or
decrypted HTTP timing. Response capture completes at provider EOF. Decrypted
response parsing remains in final verification after capture. Client response
delivery includes worker generation, verification and persistence because the
worker saves evidence before delivering the response.

## A small reproducible sample

Build a fresh candidate version from the committed source and install the wheel
non-editably, following [DEVELOPMENT.md](DEVELOPMENT.md). Use the matching public
script from that source revision. Keep test credentials, stdout, timings and
recovery state in an ignored private report directory.

```sh
WITNESS_TIMINGS=1 python examples/check_witness.py \
  --wallet-path /private/test-wallets --wallet-name witness-test \
  --wallet-hotkey test --proof-parameters /private/params \
  --model YOUR_MODEL --subscription --requests 3 \
  > /private/sample.stdout 2> /private/sample.timings.jsonl
```

`--requests` accepts 1–5 sequential requests using one `WitnessClient`.
Its default is one request, preserving the release gate's normal command.
Any failed request stops the sample; there is no automatic regeneration.
`--resume` requires `--requests=1` and retains the existing recovery behavior.
Use `OPENAI_API_KEY` through the normal secure environment setup to test API
authentication. A subscription sample does not validate API-key billing.

Compare a cold process with later `create` spans in that same process. Parameter
initialization occurs before queueing, whereas a fresh credential proof occurs
on every request. Record the operating system, CPU, whether the process runs
under emulation, wheel SHA-256, source commit, script SHA-256, gateway version,
worker image digest, exchange IDs and RunPod job IDs. The public release gate
still requires its four authentication/example cases; timing samples do not
turn a blocked case into acceptance.

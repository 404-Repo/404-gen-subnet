# Witness performance diagnostics

Set `WITNESS_TIMINGS=1` before starting the client to emit one JSON line per
completed phase to stderr. Client timings are disabled by default. The worker
image enables the same diagnostics. The Python gateway and Pod worker write
structured diagnostics to their process logs. There is no telemetry service or new
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

| Boundary | Measurements |
| --- | --- |
| Client process setup | `initialize`, `prover_initialize`, `native_library_load`, `native_initialize_call`, `http_initialize` |
| Pinned native parameter cache | `parameter_initialize` / `proof_generate` include `parameters_cached`; native steps separate authenticated file read/hash checks, circuit deserialization, key deserialization and mutex wait |
| Credentials | `subscription_credentials`, `credential_load`, conditional `credential_refresh` |
| Pod capture and recovery | worker `pod_capture`, `pod_capture_done`, `pod_recovery`, `evidence_retry_wait` |
| Storage and setup | `r2_initialize`, `client_hello_wait` |
| TLS and fixed probe | `websocket_connect`, `tls_handshake`, `certificate_validation`, `handshake_verification_wait`, `probe_response_wait`; worker `handshake_capture_wait`, `verify_hello`, `verify_handshake`, `handshake_persistence` |
| Local fresh proof | `credential_proof`, `native_prove_call`, native `proof_generate` substep; these exclude TLS and network waits |
| Worker authorization | `authorization_claim_wait`, `verify_authorize`, `verifier_subprocess`, Node `process_start_to_ready` and `verification`, native certificate/verify subprocesses, `native_verifying_key_load`, `native_proof_verify`, `authorization_persistence` |
| Provider | `provider_request_release`, `provider_first_ciphertext_wait`, `provider_response_capture` |
| Completion and delivery | `final_transcript_persistence`, `verify_finish`, `evidence_persistence`, `manifest_persistence`, `final_status_persistence`, `delivery`, client `response_delivery_wait_and_decrypt`, `parse_and_save_response`, `evidence_retrieval_wait`, `resume`, conditional `recorded_response_download` |

Record Pod creation/readiness, provider capture, evidence persistence and client
wall time separately. RunPod provides Pod identity and lifecycle. There are no
per-exchange queue-job duration fields. Never reconstruct elapsed time by
subtracting timestamps from different computers.

The provider first-byte measurement is the first TLS ciphertext received after
request release, which can include a session ticket; it is not first-token or
decrypted HTTP timing. Response capture completes at provider EOF. Decrypted
response parsing remains in final verification after capture. Client response
delivery includes worker generation, verification and persistence because the
worker saves evidence before delivering the response.

## Reproducible measurements

Build a candidate from committed source and use its matching client and runtime
manifest. Enable `WITNESS_TIMINGS=1` on the trusted runner, gateway and worker.
Keep credentials, stdout, timings and recovery state in an ignored private report
directory. The [batch runner](../miner-reference/README.md#run-several-prompts-concurrently)
provides bounded preparation and capture concurrency.

Compare cold initialization with later calls in the same process. Parameter
initialization happens once; a credential proof is prepared for each fresh
capture. Record the operating system, CPU, native versus emulated runtime, source
commit, wheel hash, gateway/worker image digest, generation and exchange IDs,
actual concurrency, retries and interruptions. Timing samples do not replace the
[release acceptance gates](../docs/witness-server-release-plan.md).

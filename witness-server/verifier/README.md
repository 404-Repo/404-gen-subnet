# Shared witness verifier

The TLS verification library runs inside the queued worker and is shared with
the local test fixtures.

- [verify.mjs](verify.mjs) verifies the handshake, disclosed request, credential
  proof, and response.
- [gate.mjs](gate.mjs) bounds the initial TLS flight and reads provider usage.
- [transport.mjs](transport.mjs) handles TLS records and the local test transport.
- [certificates.mjs](certificates.mjs) verifies the provider certificate chain.
- [worker.mjs](worker.mjs) invokes the native proof implementation from
  `witness-client/native`.

`npm test` runs the verifier tests. See
[server checks](../README.md#local-checks) for setup.

The matching `circuit.bin`, `proving.bin`, and `verifying.bin` are tracked in
[params/](params/) for client use and local tests. Their sizes and SHA-256 hashes
are pinned in [parameters.go](../../witness-client/native/parameters.go).
The server image includes only `verifying.bin`.

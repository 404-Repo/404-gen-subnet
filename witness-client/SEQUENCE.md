# Witness exchange

The miner runs `WitnessClient`. Cloudflare owns worker allocation and forwards
messages. A RunPod worker owns the provider connection, verification, and saved
evidence. R2 stores evidence under `rounds/<round>/<hotkey>/<request-id>/`.

## Allocation

```mermaid
sequenceDiagram
    participant M as Miner client
    participant G as Cloudflare gateway
    participant Q as RunPod queue
    participant W as Witness worker
    M->>M: Validate request body and local credentials
    M->>G: Signed exchange request
    G->>G: Check registration and round allowance
    G->>G: Save private allocation before dispatch
    G->>Q: Queue job with private connection capability
    Q->>W: Dispatch when a worker is available
    W->>G: Claim allocation through outbound WebSocket
    W->>G: Session metadata
    M->>G: Read session metadata
    M->>M: Check identity, provider and proof key
    M->>M: Refresh credentials after queue waiting
    M->>G: Open hotkey-signed WebSocket
```

The gateway pairs the two WebSockets. Encrypted OpenAI TLS bytes travel as binary
messages. Claims and acknowledgements travel as text messages. The client-to-gateway
and worker-to-gateway connections use their own TLS protection.

## Provider handshake and authorization

A disclosure supplies selected TLS record keys and metadata. The worker uses it
to decode the traffic it captured. Both disclosures happen before generation.

```mermaid
sequenceDiagram
    participant M as Miner client
    participant G as Gateway relay
    participant W as Witness worker
    participant O as OpenAI
    participant R as R2 evidence
    M->>G: TLS ClientHello
    G->>W: TLS ClientHello
    W->>W: Check allowed ClientHello
    W->>O: Forward ClientHello
    O-->>W: Provider handshake
    W-->>G: Provider handshake bytes
    G-->>M: Provider handshake bytes
    M->>M: Validate provider certificate and handshake
    M->>G: Client Finished, fixed HEAD probe, first disclosure
    G->>W: Forward bytes and disclosure
    W->>W: Verify handshake and exact unauthenticated probe
    W->>R: Save handshake evidence
    W-->>M: Handshake accepted through gateway
    W->>O: Release Finished and HEAD probe
    O-->>M: Probe response through worker and gateway
    M->>M: Isolate credential records and generate proof
    M->>G: Encrypted generation request and authorization proof
    G->>W: Forward bytes and proof
    W->>W: Verify request, credential proof and response key
    W->>R: Save request, authorization and observed traffic
    W-->>M: Authorization accepted through gateway
    W->>O: Release generation request
```

The credential's encryption key stays in the miner process. The native proof
binds the hidden credential records to the observed TLS connection. The worker
receives the response key before releasing the request, so capture can continue
without further miner disclosures.

## Capture and recovery

```mermaid
sequenceDiagram
    participant M as Miner client
    participant G as Cloudflare gateway
    participant W as Witness worker
    participant O as OpenAI
    participant R as R2 evidence
    O-->>W: Generated response
    W->>W: Capture and verify response and usage
    W->>R: Save transcript, response, manifest and issued status
    alt Miner connection remains open
        W-->>G: Recorded response TLS bytes and end marker
        G-->>M: Recorded response TLS bytes and end marker
        M->>M: Decode typed OpenAI response
    else Miner connection was lost
        M->>G: resume with saved recovery state
        G->>R: Read owned status and response
        R-->>G: Saved evidence
        G-->>M: Recorded result
    end
    W->>G: Allocation finished
```

A broken live connection is not resumed as a TLS stream. `resume()` retrieves
an authorized result from retained evidence. An open connection that stays
silent after the status is `issued` is closed and retrieved the same way.
`release(exchange_id)` retires a stuck allocation, allowing a later `create()`
to queue another job. It does not reset the job allowance or cancel provider
capture already in progress.

The private allocation survives gateway restarts. Repeated queue calls keep the
same request ID. A worker capability can be claimed once, and a retired request
cannot accept a late worker. A new RunPod job may reuse a warm worker.

During validation, the validator lists evidence for the round and hotkey, runs
the submitted miner code, compares its outgoing GPT requests with the recorded
requests, and returns recorded responses instead of calling GPT.

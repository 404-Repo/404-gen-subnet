# Witness capture

```mermaid
sequenceDiagram
    participant M as Submitted generate()
    participant H as Execution-host witness boundary
    participant W as Authenticated witness Pod
    participant P as OpenAI
    participant R as Private evidence store
    M->>H: responses.create(parameters, request_id)
    H->>H: Save per-key occurrence and entry observation
    H->>W: POST /v1/calls with logical key (allocation supplies miner/round)
    W-->>H: 202 with assigned/existing attempt, or 200 with saved result
    H->>W: TLS handshake and hidden credential proof
    W->>R: Authorize and fence possible dispatch durably
    W->>P: Release one verified provider request
    P-->>W: Provider response/failure
    W->>R: Save TLS evidence, usage, manifest and settlement
    H->>W: Poll GET /v1/calls/{exchange_id} when needed
    W-->>H: Durable result or witness failure
    H->>H: Save exit/outcome before exposing it
    H-->>M: Response, buffered events or original error
```

An already issued key returns its retained result without provider inference.
Only proven non-dispatch permits another capture attempt. A lost dispatch
acknowledgement or partial authorized response remains uncertain.

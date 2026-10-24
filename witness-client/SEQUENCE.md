# Witness request and recovery sequence

The trusted runner binds miner code to frozen generation inputs. The Python
gateway authenticates the hotkey and coordinates ordinary CPU Pods. Durable R2
claims prevent duplicate provider dispatch; recordings preserve invocation and
delivery order.

```mermaid
sequenceDiagram
    participant M as Generation-bound client
    participant G as Python gateway
    participant W as Pod worker
    participant R as Private R2
    participant O as OpenAI
    M->>G: Reserve invocation with signed hotkey request
    G->>R: Reserve call identity and invocation position
    alt Recorded response exists
        G->>R: Read verified recording
        G-->>M: Deliver in recorded order
    else Capture required
        M->>G: Dispatch to selected exact Pod
        G->>W: Assign exchange
        M->>G: Open authenticated WebSocket
        G->>W: Forward TLS records and proof messages
        W->>O: TLS handshake and fixed probe
        W-->>M: Handshake records and disclosure request
        M->>W: Credential proof and request disclosure through gateway
        W->>R: Persist authorization and dispatch allowance
        W->>O: Release billable request bytes
        O-->>W: Encrypted response
        W->>R: Persist transcript, verified usage and manifest
        W-->>M: Deliver retained response through gateway
        M->>G: Record delivery receipt and generation output
        G->>R: Save recording
    end
```

The worker saves authorization before releasing billable bytes. After release,
provider capture and persistence continue independently of the miner connection.
Upload retries use the saved spool. A disconnected or timed-out client keeps the
same exchange and generation identity.

```mermaid
sequenceDiagram
    participant M as Client after interruption
    participant G as Python gateway
    participant W as Existing Pod capture
    participant R as Private R2
    M->>G: Resume saved exchange or repeat generation prefix
    G->>R: Read durable call state
    alt Capture is unresolved
        G->>W: Join existing capture when available
        G-->>M: Wait or report pending state
    else Recording is issued
        G->>R: Read and verify retained response
        G-->>M: Return original response in recorded order
    end
```

The revivor can replace a failed gateway while workers finish their captures.
RunPod is authoritative for Pod ownership and lifecycle; R2 retains inference
claims, attempts and usage. See [PODS.md](../witness-gateway/PODS.md) for fencing,
replacement and unresolved-usage rules.

Offline replay uses native-verified recordings and retained provider responses.
It checks request identity, invocation order, delivery order and generated output
without provider access. Renderer replay uses the pinned renderer and unchanged
validation rules.

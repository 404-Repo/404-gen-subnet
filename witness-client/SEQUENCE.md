# Witness request and recovery sequence

The trusted runner binds miner code to frozen generation inputs. The Python
gateway authenticates registration and allocates ordinary CPU Pods. Durable R2
claims prevent duplicate provider dispatch; recordings preserve invocation and
delivery order.

```mermaid
sequenceDiagram
    participant M as Generation-bound client
    participant G as Python gateway
    participant W as Assigned Pod TLS service
    participant R as Private R2
    participant O as OpenAI
    M->>G: Resolve allocation with signed hotkey request
    G-->>M: Public TCP mapping, verified TLS name/CA, scoped grant
    G->>R: Fence selected exact Pod allocation
    G->>W: Lifecycle activation
    M->>W: Reserve invocation directly over authenticated TLS
    W->>R: Reserve call identity and invocation position
    alt Recorded response exists
        W-->>M: Trusted completion hashes and signed evidence URLs
        M->>R: Download missing bytes directly and verify hashes
    else Capture required
        M->>W: Assign and open direct authenticated WebSocket
        W->>R: Check allocation fence and claim exchange
        W->>O: TLS handshake and fixed probe
        W-->>M: Handshake records and disclosure request
        M->>W: Credential proof and request disclosure
        W->>R: Persist authorization and dispatch allowance
        W->>O: Release billable request bytes
        O-->>W: Encrypted response
        W->>R: Persist immutable evidence, usage and completed call
        W-->>M: Deliver response and trusted completion metadata directly
        M->>M: Check received bytes against completion hashes
    end
    M->>W: Record delivery receipt and generation output
    W->>R: Save recording
```

The worker saves authorization before releasing billable bytes. After release,
provider capture and persistence continue independently of the miner connection.
Upload retries use the saved spool. A disconnected or timed-out client keeps the
same exchange and generation identity.

```mermaid
sequenceDiagram
    participant M as Client after interruption
    participant G as Python gateway
    participant W as Assigned Pod TLS service
    participant R as Private R2
    opt Endpoint requires resolution or grant renewal
        M->>G: Resolve existing allocation or safely replace a lost Pod
        G-->>M: Verified direct endpoint and scoped grant
    end
    M->>W: Resume saved exchange or repeat generation prefix
    W->>R: Read durable call state
    alt Capture is unresolved
        W-->>M: Wait or report pending state
    else Recording is issued
        W-->>M: Trusted completion hashes and signed object URLs
        M->>R: Download original response directly
        M->>M: Verify binding, size and hashes
        M->>W: Consume original delivery receipt
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

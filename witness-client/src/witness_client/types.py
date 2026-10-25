"""Configuration and data exchanged by the witness client."""

from typing import Literal, NotRequired, TypeAlias, TypedDict

# Recursive JSON values used in request bodies and gateway messages.
JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)
JSONObject: TypeAlias = dict[str, JSONValue]


class Credentials(TypedDict, total=False):
    """Authentication arguments passed to WitnessClient; choose exactly one.

    Attributes:
        openai_api_key: API key used for the OpenAI Responses endpoint.
        subscription: "codex" to use the local ChatGPT subscription login.
    """

    openai_api_key: str
    subscription: Literal["codex"]


class WitnessConfig(TypedDict):
    """Client configuration supplied as a mapping or loaded from a JSON file.

    Attributes:
        witness_url: HTTPS origin of the subnet's public witness gateway.
        proof_parameters: Directory containing the pinned proof parameter files.
        state_directory: Private directory for exchange recovery files. Defaults
            to 404-witness-client under XDG_STATE_HOME or ~/.local/state.
        codex_home: Subscription login directory; defaults to CODEX_HOME or
            ~/.codex.
        codex: Codex executable name or path for login refresh; defaults to codex.
        round: Pinned competition round.
    """

    witness_url: str
    proof_parameters: str
    state_directory: NotRequired[str]
    codex_home: NotRequired[str]
    codex: NotRequired[str]
    round: str


class Session(TypedDict):
    """Assigned worker metadata saved locally for recovery.

    The client reads it through the gateway before starting provider TLS,
    checking the protocol, provider, proof key, and miner against its own values.

    Attributes:
        protocol: Witness protocol version (2).
        id: Client-generated exchange UUID used for transport and recovery.
        provider: Full OpenAI endpoint selected by the signed provider choice.
        proof_key_sha256: SHA-256 of the worker's verifying.bin, matched to
            the client's proof parameters.
        round: Competition round assigned by the gateway.
        miner: Authenticated miner's SS58 hotkey.
    """

    protocol: int
    id: str
    provider: str
    proof_key_sha256: str
    round: str
    miner: str


class ExchangeStatus(TypedDict):
    """Worker progress read from /exchanges/{id}/progress through the gateway.

    Attributes:
        status: Exchange phase: new, running, issued, or failed. Issued means the
            evidence is saved, including when OpenAI rejected the request.
        provider_status: OpenAI HTTP response status, once recorded.
        handshook: Whether the worker verified the TLS setup disclosure.
        authorized: Whether request authorization was saved before generation.
        evidence_expires: Evidence expiry as Unix time in milliseconds.
    """

    status: str
    provider_status: NotRequired[int | None]
    handshook: NotRequired[bool]
    authorized: NotRequired[bool]
    evidence_expires: NotRequired[int | None]


class PlainRecord(TypedDict):
    """Mark a TLS record sent without encryption, such as ClientHello.

    plain=True tells the verifier to read the record directly.
    """

    plain: Literal[True]


class EncryptedRecord(TypedDict):
    """Decryption information for a disclosed TLS record.

    Attributes:
        key: 32-byte ChaCha20 encryption key, in hexadecimal.
        iv: 12-byte TLS base IV, in hexadecimal. The record nonce combines this
            IV with the sequence number.
        number: Record sequence number under this key; resets when keys change.
    """

    key: str
    iv: str
    number: int


class HiddenRecord(TypedDict):
    """Empty marker for a credential record or KeyUpdate whose key stays private."""


# Each captured TLS record has one of these disclosure forms.
RecordDisclosure: TypeAlias = PlainRecord | EncryptedRecord | HiddenRecord


class Disclosures(TypedDict):
    """Disclosures in captured TLS record order, grouped by traffic direction.

    Attributes:
        client: Miner-to-OpenAI records.
        server: OpenAI-to-miner records.
    """

    client: list[RecordDisclosure]
    server: list[RecordDisclosure]


class PrivateRecord(TypedDict):
    """Local prover input for a hidden TLS record; kept in the client.

    Attributes:
        key: Hidden record's 32-byte encryption key, in hexadecimal.
        nonce: 12-byte TLS base IV, in hexadecimal, like EncryptedRecord.iv.
        number: Record sequence number under the private key.
        record: Full encrypted TLS record, including header and authentication
            tag, in hexadecimal.
    """

    key: str
    nonce: str
    number: int
    record: str


class ProofBundle(TypedDict):
    """Native proofs for the credential record and its following KeyUpdate.

    Attributes:
        commitment: Hexadecimal MiMC commitment binding both records' proofs to
            their shared private encryption key.
        mac_keys: Hexadecimal Poly1305 authentication keys, ordered credential
            record then KeyUpdate.
        proofs: Base64 Groth16 proofs. For each record, an authentication-key
            proof comes first, followed by ciphertext-chunk proofs.
    """

    commitment: str
    mac_keys: list[str]
    proofs: list[str]


class CredentialProof(TypedDict):
    """Proof material sent to the witness to verify the hidden credential records.

    Attributes:
        nonce: Private key epoch's base IV, in hexadecimal.
        bundle: Proofs for the credential record and its following KeyUpdate.
    """

    nonce: str
    bundle: ProofBundle


class HandshakeClaim(TypedDict):
    """Serialized TLS setup disclosure sent to the worker for verification.

    Attributes:
        disclosures: Disclosures as UTF-8 JSON, raw-DEFLATE compressed and base64
            encoded.
        response_key: Initial server application-record key at sequence zero,
            disclosed before generation so the worker can decode responses.
        client_bytes: Total client TLS bytes queued for this phase, including
            record headers. The worker waits for this many bytes to verify them.
        probe: Marks the fixed unauthenticated HEAD request used during setup.
    """

    disclosures: str
    response_key: EncryptedRecord
    client_bytes: int
    probe: Literal[True]


class Claim(HandshakeClaim):
    """Request authorization checked before the worker forwards generation.

    Inherits the handshake claim's fields and adds credential, the proof for the
    hidden credential record and its following KeyUpdate.
    """

    credential: CredentialProof


class HandshakeDisclosure(TypedDict):
    """TLS setup data passed from the transport to the handshake callback.

    Fields have the same meanings as HandshakeClaim, except disclosures is the
    structured record list before compression and base64 encoding.
    """

    disclosures: Disclosures
    response_key: EncryptedRecord
    client_bytes: int
    probe: Literal[True]


class EarlyDisclosure(HandshakeDisclosure):
    """Transport data passed to the authorization callback before generation.

    private holds the credential record and its following KeyUpdate. The client
    uses them to produce CredentialProof, then sends the worker a Claim.
    """

    private: list[PrivateRecord]


class InputTokenDetails(TypedDict, total=False):
    """Input token breakdown from OpenAI usage.

    cached_tokens counts tokens served from the prompt cache; these are already
    included in Usage.input_tokens. cache_write_tokens counts input tokens
    written to the cache.
    """

    cached_tokens: int
    cache_write_tokens: int


class OutputTokenDetails(TypedDict, total=False):
    """Output token breakdown from OpenAI usage.

    reasoning_tokens counts reasoning tokens already included in
    Usage.output_tokens.
    """

    reasoning_tokens: int


class Usage(TypedDict, total=False):
    """OpenAI's reported token counts in captured usage evidence.

    Attributes:
        input_tokens: Total input tokens, including cached input.
        output_tokens: Total output tokens, including reasoning.
        total_tokens: Sum of input and output tokens.
        input_tokens_details: Breakdown of input tokens.
        output_tokens_details: Breakdown of output tokens.
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_tokens_details: InputTokenDetails
    output_tokens_details: OutputTokenDetails


class OutputContent(TypedDict, total=False):
    """OpenAI message content part; output_text parts carry the text field."""

    type: str
    text: str


class OutputItem(TypedDict, total=False):
    """OpenAI output item; message items carry a list of content parts."""

    type: str
    content: list[OutputContent]


class CompletedResponse(TypedDict, total=False):
    """Fields read from the terminal OpenAI response by the stream decoder.

    Attributes:
        status: Terminal generation status: completed, failed, or incomplete.
        output: Generated output items, including message content.
        usage: Provider-reported token counts, when available.
    """

    status: str
    output: list[OutputItem]
    usage: Usage | None


class ResponseEvent(TypedDict, total=False):
    """Fields read from each OpenAI server-sent event by the stream decoder.

    Attributes:
        type: Event name, such as response.output_text.delta or response.completed.
        output_index: Position of the output item this text belongs to.
        content_index: Position of the content part within that output item.
        delta: Text fragment from a response.output_text.delta event.
        text: Finished text from a response.output_text.done event.
        item: Completed output item from a response.output_item.done event.
        response: Response object carried by a terminal event.
    """

    type: str
    output_index: int
    content_index: int
    delta: str
    text: str
    item: OutputItem
    response: CompletedResponse

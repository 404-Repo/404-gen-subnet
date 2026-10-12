"""Protocol records and persisted fields shared with existing deployments."""

from __future__ import annotations

from typing import Literal, NotRequired, Protocol, TypedDict

from bindings import Bucket, KV, Namespace

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
type Role = Literal["miner", "worker"]
type JobState = Literal[
    "submitting", "queued", "uncertain", "connected", "finished", "retired"
]


class Env(Protocol):
    ROUND: str
    NETWORK: str
    NETUID: str
    SUBTENSOR_RPC_URL: str
    ROUND_END: str
    RUNPOD_ENDPOINT: str
    RUNPOD_API_KEY: str
    MAX_JOBS_PER_MINER: str
    TEST_HOTKEY: str
    MINERS: Namespace
    REGISTRY: KV
    TRANSCRIPTS: Bucket


class Exchange(TypedDict):
    operation: Literal["exchange"]
    hotkey: str
    receiver: str
    exchange_id: str
    deadline: int
    provider: Literal["api", "codex"]
    signature: str
    replace: NotRequired[str]


class GatewayRequest(TypedDict):
    hotkey: str
    nonce: str
    requestId: str
    exchange: NotRequired[Exchange]
    object: NotRequired[str]
    connect: NotRequired[bool]
    release: NotRequired[bool]
    origin: NotRequired[str]


class Job(TypedDict):
    digest: str
    state: JobState
    deadline: int
    tokenHash: str
    claimed: bool
    minerAttached: bool
    runpodId: NotRequired[str]
    session: NotRequired[dict[str, Json]]


class Connection(TypedDict):
    requestId: str
    role: Role
    bytes: int
    messages: int


class WorkerConnection(TypedDict):
    requestId: str
    token: str


class Rate(TypedDict):
    at: int
    tokens: float


class Membership(TypedDict):
    version: Literal[1]
    network: Literal["finney"]
    netuid: int
    block: int
    block_hash: str
    block_time: int
    fetched_at: int
    hotkeys: list[str]


class Rejection(Exception):
    def __init__(
        self, status: int, message: str, exchange_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.exchange_id = exchange_id

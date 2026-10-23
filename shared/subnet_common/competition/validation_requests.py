from collections.abc import Iterator

from pydantic import BaseModel, ConfigDict, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class ValidationRequest(BaseModel):
    """
    Request for one candidate-validation pass in this file's round.

    Written by judge-service when a miner becomes a local winner.
    Consumed by the validator; independent of tournament defenders and source audits.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hotkey: str


ValidationRequestListAdapter = TypeAdapter(list[ValidationRequest])


class ValidationRequests:
    """Append-only list of validation requests. Deduplicates by hotkey."""

    def __init__(self) -> None:
        self._requests: dict[str, ValidationRequest] = {}

    def add(self, request: ValidationRequest) -> bool:
        """Request validation once per hotkey. Returns True only for a new request."""
        if request.hotkey in self._requests:
            return False
        self._requests[request.hotkey] = request
        return True

    def has(self, hotkey: str) -> bool:
        return hotkey in self._requests

    def has_any(self) -> bool:
        return bool(self._requests)

    def get(self, hotkey: str) -> ValidationRequest | None:
        return self._requests.get(hotkey)

    def __iter__(self) -> Iterator[ValidationRequest]:
        return iter(self._requests.values())

    @property
    def hotkeys(self) -> set[str]:
        return set(self._requests.keys())

    def __len__(self) -> int:
        return len(self._requests)

    def to_json(self) -> str:
        return ValidationRequestListAdapter.dump_json(list(self._requests.values()), indent=2).decode()

    @classmethod
    def from_json(cls, content: str) -> "ValidationRequests":
        requests = cls()
        for r in ValidationRequestListAdapter.validate_json(content):
            requests.add(r)
        return requests


async def get_validation_requests(git: GitHubClient, round_num: int, ref: str) -> ValidationRequests:
    content = await git.get_file(f"rounds/{round_num}/validation_requests.json", ref=ref)
    if content is None:
        return ValidationRequests()
    return ValidationRequests.from_json(content)


async def save_validation_requests(git_batcher: GitBatcher, round_num: int, requests: ValidationRequests) -> None:
    await git_batcher.write(
        path=f"rounds/{round_num}/validation_requests.json",
        content=requests.to_json(),
        message=f"Update validation requests for round {round_num}",
    )

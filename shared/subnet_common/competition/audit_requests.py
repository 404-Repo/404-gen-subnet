from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class AuditRequest(BaseModel):
    """
    Request for miner verification.

    Written by judge-service when a miner becomes a local winner.
    Read by output-verifier to know what to verify.

    Critical prompts are stems (filenames without extension) of decisive prompts.
    These have stricter tolerance (max 1 mismatch allowed).
    """

    hotkey: str
    critical_prompts: list[str] = Field(description="Stems of decisive prompts")


AuditRequestListAdapter = TypeAdapter(list[AuditRequest])


class AuditRequests:
    """Append-only list of audit requests. Deduplicates by hotkey."""

    def __init__(self) -> None:
        self._requests: list[AuditRequest] = []
        self._hotkeys: set[str] = set()

    def add(self, request: AuditRequest) -> bool:
        """Add request if hotkey not present. Returns True if added."""
        if request.hotkey in self._hotkeys:
            return False
        self._requests.append(request)
        self._hotkeys.add(request.hotkey)
        return True

    def has(self, hotkey: str) -> bool:
        return hotkey in self._hotkeys

    def has_any(self) -> bool:
        return bool(self._requests)

    def get(self, hotkey: str) -> AuditRequest | None:
        for r in self._requests:
            if r.hotkey == hotkey:
                return r
        return None

    @property
    def hotkeys(self) -> set[str]:
        return self._hotkeys.copy()

    def __len__(self) -> int:
        return len(self._requests)

    def to_json(self) -> str:
        return AuditRequestListAdapter.dump_json(self._requests, indent=2).decode()

    @classmethod
    def from_json(cls, content: str) -> "AuditRequests":
        requests = cls()
        for r in AuditRequestListAdapter.validate_json(content):
            requests.add(r)
        return requests


async def get_audit_requests(git: GitHubClient, round_num: int, ref: str) -> AuditRequests:
    content = await git.get_file(f"rounds/{round_num}/require_audit.json", ref=ref)
    if not content:
        return AuditRequests()
    return AuditRequests.from_json(content)


async def save_audit_requests(git_batcher: GitBatcher, round_num: int, requests: AuditRequests) -> None:
    await git_batcher.write(
        path=f"rounds/{round_num}/require_audit.json",
        content=requests.to_json(),
        message=f"Update audit requests for round {round_num}",
    )

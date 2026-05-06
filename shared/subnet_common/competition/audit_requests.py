from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class AuditRequest(BaseModel):
    """
    Request for miner verification.

    Written by judge-service when a miner becomes a local winner.
    Read by the generation orchestrator to know what to regenerate.
    """

    hotkey: str
    latest_defender: str | None = Field(
        default=None,
        description=(
            "Hotkey of the most recent leader the audited miner has beaten ('leader' "
            "for the round-leader, or another miner's hotkey for a timeline win). Updated "
            "in place as the miner climbs the timeline — re-requesting an audit doesn't "
            "re-trigger orchestrator regeneration but does refresh this field. Used by "
            "judge-service for the informational generated-vs-defender duel after the "
            "verification audit. None on entries written before this field was introduced."
        ),
    )


AuditRequestListAdapter = TypeAdapter(list[AuditRequest])


class AuditRequests:
    """Append-only list of audit requests. Deduplicates by hotkey."""

    def __init__(self) -> None:
        self._requests: dict[str, AuditRequest] = {}

    def add(self, request: AuditRequest) -> bool:
        """Add request if hotkey not present. Returns True if added.

        Does NOT update an existing entry — callers that want to refresh `latest_defender`
        on a re-request use `update_latest_defender` after `add` returns False.
        """
        if request.hotkey in self._requests:
            return False
        self._requests[request.hotkey] = request
        return True

    def update_latest_defender(self, hotkey: str, latest_defender: str) -> bool:
        """Refresh `latest_defender` on an existing request. Returns True if it changed.

        No-op (returns False) when the hotkey isn't in the dict or the value is unchanged
        — callers can use the boolean to decide whether re-persistence is needed.
        """
        existing = self._requests.get(hotkey)
        if existing is None or existing.latest_defender == latest_defender:
            return False
        existing.latest_defender = latest_defender
        return True

    def has(self, hotkey: str) -> bool:
        return hotkey in self._requests

    def has_any(self) -> bool:
        return bool(self._requests)

    def get(self, hotkey: str) -> AuditRequest | None:
        return self._requests.get(hotkey)

    @property
    def hotkeys(self) -> set[str]:
        return set(self._requests.keys())

    def __len__(self) -> int:
        return len(self._requests)

    def to_json(self) -> str:
        return AuditRequestListAdapter.dump_json(list(self._requests.values()), indent=2).decode()

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

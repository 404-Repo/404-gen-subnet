"""Verification verdict enum.

Verdicts are derived on-demand by judge-service from the duel artifacts on disk
(`audit_submitted.json` + `audit_{defender[:10]}.json`) and the audit request's current
`latest_defender`. The verdict is never persisted — only the underlying duel reports
are. See `judge_service.audit_execution.compute_verification`.
"""

from enum import StrEnum


class VerificationOutcome(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"

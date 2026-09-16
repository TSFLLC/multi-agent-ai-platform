"""Approval Service — Section 11, 23. Evaluates approval policy,
creates/blocks/resolves Approval records. PR merge is V1's initial
consequential-action acceptance case (Owner decision); real protected-
branch merge stays disabled until Git Tool safety/approval testing
completes. Real implementation starts MA3 (cross-cutting)."""

from app.services.base import BaseService


class ApprovalService(BaseService):
    def request_approval(self, *args, **kwargs):
        """Computes and stores action_fingerprint (Section 24.4 #15)."""
        raise NotImplementedError("ApprovalService lands in MA3")

    def resolve(self, *args, **kwargs):
        """Must re-verify the caller-echoed action_fingerprint against the
        stored one, and the gated transition must re-verify it again at
        execution time — a stale approval is rejected, never honored."""
        raise NotImplementedError("ApprovalService lands in MA3")

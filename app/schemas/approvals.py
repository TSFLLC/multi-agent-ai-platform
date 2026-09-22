from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.db.enums import ApprovalScope, ApprovalStatus
from app.schemas.common import ORMModel


class ApprovalRead(ORMModel):
    id: str
    scope: ApprovalScope
    scope_ref_id: str
    operation_type: str
    action_fingerprint: str
    status: ApprovalStatus
    requested_at: datetime
    expires_at: Optional[datetime] = None
    # MA7.3a additions (all optional/additive): what is being approved and,
    # once decided, the immutable decision provenance.
    bound_artifact_id: Optional[str] = None
    requested_by: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolution_note: Optional[str] = None


class ApprovalEvidenceItem(BaseModel):
    """One bound output. Metadata only -- the artifact CONTENT is served, with
    its own authorization, by ``GET /artifacts/{artifact_id}/content``."""

    node_key: str
    node_run_id: str
    artifact_id: Optional[str] = None
    content_hash: Optional[str] = None
    # The artifact still exists and its file still hashes to ``content_hash``.
    content_intact: bool


class ApprovalEvidenceRead(BaseModel):
    """MA7.4c: the canonical evidence (source ``node_key`` ascending) a workflow
    gate's approval binds, and whether it still matches the approval's
    fingerprint. ``fingerprint_matches`` False means a bound output changed,
    was replaced, deleted or tampered with -- approving would be refused."""

    approval_id: str
    workflow_node_run_id: str
    evidence_version: int
    action_fingerprint: str
    fingerprint_matches: bool
    evidence: List[ApprovalEvidenceItem]


class ApprovalResolveRequest(BaseModel):
    """Section 25.5 — the approver UI must echo the fingerprint it displayed;
    server rejects with 409 fingerprint_mismatch if the action changed."""

    approve: bool
    action_fingerprint: str = Field(min_length=1, max_length=128)
    notes: Optional[str] = Field(default=None, max_length=4000)

from datetime import datetime
from typing import Optional

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


class ApprovalResolveRequest(BaseModel):
    """Section 25.5 — the approver UI must echo the fingerprint it displayed;
    server rejects with 409 fingerprint_mismatch if the action changed."""

    approve: bool
    action_fingerprint: str = Field(min_length=1, max_length=128)
    notes: Optional[str] = Field(default=None, max_length=4000)

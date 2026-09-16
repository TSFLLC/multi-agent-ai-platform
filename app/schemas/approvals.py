from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.db.enums import ApprovalScope, ApprovalStatus


class ApprovalRead(BaseModel):
    id: str
    scope: ApprovalScope
    scope_ref_id: str
    operation_type: str
    action_fingerprint: str
    status: ApprovalStatus
    requested_at: datetime
    expires_at: Optional[datetime] = None


class ApprovalResolveRequest(BaseModel):
    """Section 25.5 — the approver UI must echo the fingerprint it displayed;
    server rejects with 409 fingerprint_mismatch if the action changed."""

    approve: bool
    action_fingerprint: str
    notes: Optional[str] = None

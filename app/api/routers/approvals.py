"""Approval resource boundary — Section 25.2, 25.5, 23. PR merge (Owner
decision) is V1's initial consequential-action acceptance case; real
protected-branch merge stays disabled until Git Tool safety testing."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.schemas.approvals import ApprovalRead, ApprovalResolveRequest
from app.schemas.common import FingerprintMismatch

router = APIRouter(tags=["approvals"])


@router.get("/approvals", response_model=List[ApprovalRead])
def list_approvals(db: Session = Depends(get_db), status: Optional[str] = Query(default="pending")):
    not_implemented()


@router.post(
    "/approvals/{approval_id}/resolve",
    response_model=ApprovalRead,
    responses={409: {"model": FingerprintMismatch, "description": "fingerprint_mismatch"}},
)
def resolve_approval(approval_id: str, body: ApprovalResolveRequest, db: Session = Depends(get_db)):
    """The caller must echo the action_fingerprint it displayed; a mismatch
    against the stored approvals.action_fingerprint is rejected with 409
    (Section 24.4 #15) — re-checked again at execution time, not only here."""
    not_implemented()


@router.get("/audit-events")
def list_audit_events(db: Session = Depends(get_db)):
    """Admin/Owner-only, RBAC-restricted (Section 20.5, 25.5) — separate
    from GET /events (Flight Recorder). RBAC enforcement lands in MA1; this
    is contract-only in MA0."""
    not_implemented()

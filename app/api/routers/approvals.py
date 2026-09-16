"""Approval resource boundary — Section 25.2, 25.5, 23. PR merge (Owner
decision) is V1's initial consequential-action acceptance case; real
protected-branch merge stays disabled until Git Tool safety testing."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.authz import require_org_admin
from app.models.identity import User
from app.schemas.approvals import ApprovalRead, ApprovalResolveRequest
from app.schemas.common import FingerprintMismatch
from app.schemas.events import AuditEventRead
from app.services.audit_service import AuditService

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


@router.get("/audit-events", response_model=List[AuditEventRead])
def list_audit_events(
    org_id: str = Query(...),
    db: Session = Depends(get_db),
    _caller: User = Depends(require_org_admin),
):
    """Structurally separate from GET /events (Flight Recorder) — Section
    20.5, 25.5. Admin/Owner-only (MA1B): require_org_admin checks the
    caller's own users.role AND that org_id matches the caller's own
    org_id — an arbitrary/other org_id is rejected the same way an
    unauthorized one is, never used to bypass authorization."""
    return AuditService(db).list_for_org(org_id=org_id)

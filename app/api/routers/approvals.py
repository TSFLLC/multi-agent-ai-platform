"""Approval resource boundary — Section 25.2, 25.5, 23. PR merge (Owner
decision) is V1's initial consequential-action acceptance case; real
protected-branch merge stays disabled until Git Tool safety testing.

MA7.3a (Approval Core): ``GET /approvals``, ``GET /approvals/{id}`` and
``POST /approvals/{id}/resolve`` are real. Viewing requires READ on the
approval's owning project; approving/rejecting requires MODIFY. Authorization
is enforced inside ``ApprovalService`` (the one place decisions are made), so
these handlers stay thin and cannot bypass it."""

from typing import List, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.authz import ProjectAction, require_org_admin
from app.db.enums import ApprovalStatus
from app.models.identity import User
from app.schemas.approvals import ApprovalRead, ApprovalResolveRequest
from app.schemas.common import FingerprintMismatch
from app.schemas.events import AuditEventRead
from app.services.approval_service import ApprovalService
from app.services.audit_service import AuditService

router = APIRouter(tags=["approvals"])


@router.get("/approvals", response_model=List[ApprovalRead])
def list_approvals(
    project_id: str = Query(...),
    status: Literal["pending", "approved", "rejected", "expired", "all"] = Query(default="pending"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """One project's approvals, newest first. ``status`` is one of
    pending (default), approved, rejected, expired, or ``all``. Requires READ
    on ``project_id``."""
    status_filter = None if status == "all" else ApprovalStatus(status)
    approvals = ApprovalService(db).list_for_user(
        user=user, project_id=project_id, status=status_filter, limit=limit
    )
    return [ApprovalRead.model_validate(a) for a in approvals]


@router.get("/approvals/{approval_id}", response_model=ApprovalRead)
def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Requires READ on the approval's owning project."""
    approval = ApprovalService(db).get_for_user(approval_id, user=user, action=ProjectAction.READ)
    return ApprovalRead.model_validate(approval)


@router.post(
    "/approvals/{approval_id}/resolve",
    response_model=ApprovalRead,
    responses={409: {"model": FingerprintMismatch, "description": "fingerprint_mismatch"}},
)
def resolve_approval(
    approval_id: str,
    body: ApprovalResolveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Approve or reject. Requires MODIFY on the owning project.

    The caller must echo the action_fingerprint it displayed; a mismatch
    against the stored approvals.action_fingerprint is rejected with 409
    fingerprint_mismatch (Section 24.4 #15). Repeating the same decision is an
    idempotent 200; the opposite decision (or any decision on an expired
    approval) is 409 invalid_state_transition."""
    approval = ApprovalService(db).resolve(
        approval_id,
        user=user,
        approve=body.approve,
        action_fingerprint=body.action_fingerprint,
        note=body.notes,
    )
    return ApprovalRead.model_validate(approval)


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

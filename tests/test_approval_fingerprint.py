"""Approval action-fingerprint binding — Section 24.4 #15, Acceptance Criterion 12."""

import hashlib

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import ApprovalScope, ApprovalStatus
from app.models.governance import Approval


def _fingerprint(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


def test_action_fingerprint_is_required(db):
    approval = Approval(
        scope=ApprovalScope.AGENT_RUN,
        scope_ref_id="x",
        operation_type="pr_merge",
        action_fingerprint=None,
        status=ApprovalStatus.PENDING,
    )
    db.add(approval)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_approval_binds_to_exact_fingerprint(db):
    original_diff = "diff --git a/x.py b/x.py\n+print(1)\n"
    fp = _fingerprint(original_diff)
    approval = Approval(
        scope=ApprovalScope.AGENT_RUN,
        scope_ref_id="run-1",
        operation_type="pr_merge",
        action_fingerprint=fp,
        status=ApprovalStatus.PENDING,
    )
    db.add(approval)
    db.commit()

    assert approval.action_fingerprint == fp


def test_changed_action_produces_a_different_fingerprint():
    """This is the TOCTOU scenario Section 24.4 #15 closes: if the PR gains
    new commits between approval and merge, recomputing the fingerprint at
    execution time must not match the stored one — the gated transition
    (a later-phase concern) is expected to reject on this mismatch."""
    original = _fingerprint("diff v1")
    changed = _fingerprint("diff v2")
    assert original != changed


def test_resolved_approval_records_resolver_and_timestamp(db):
    from datetime import datetime, timezone

    approval = Approval(
        scope=ApprovalScope.TASK_RUN,
        scope_ref_id="run-2",
        operation_type="pr_merge",
        action_fingerprint=_fingerprint("diff"),
        status=ApprovalStatus.PENDING,
    )
    db.add(approval)
    db.commit()

    approval.status = ApprovalStatus.APPROVED
    approval.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(approval)

    assert approval.status == ApprovalStatus.APPROVED
    assert approval.resolved_at is not None

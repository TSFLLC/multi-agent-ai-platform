"""Durable idempotency — Section 24.4 #11, 25.4, 28.3."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import IdempotencyScope, IdempotencyStatus
from app.models.execution import IdempotencyKey


def test_idempotency_key_is_unique(db):
    db.add(
        IdempotencyKey(
            key="req-123",
            scope=IdempotencyScope.API_REQUEST,
            resource_type="task_run",
            status=IdempotencyStatus.IN_PROGRESS,
        )
    )
    db.commit()

    db.add(
        IdempotencyKey(
            key="req-123",
            scope=IdempotencyScope.API_REQUEST,
            resource_type="task_run",
            status=IdempotencyStatus.IN_PROGRESS,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_idempotency_key_transitions_to_completed_with_result(db):
    key = IdempotencyKey(
        key="tool-call-456",
        scope=IdempotencyScope.TOOL_CALL,
        resource_type="tool_call",
        status=IdempotencyStatus.IN_PROGRESS,
    )
    db.add(key)
    db.commit()

    key.status = IdempotencyStatus.COMPLETED
    key.result_ref = {"tool_call_id": "abc"}
    db.commit()
    db.refresh(key)

    assert key.status == IdempotencyStatus.COMPLETED
    assert key.result_ref == {"tool_call_id": "abc"}


def test_api_request_and_tool_call_scopes_share_one_table(db):
    """Both Section 25.4 (API Idempotency-Key header) and Section 28.3
    (side-effecting tool dedup) resolve against this single table."""
    db.add(
        IdempotencyKey(
            key="a",
            scope=IdempotencyScope.API_REQUEST,
            resource_type="task_run",
            status=IdempotencyStatus.COMPLETED,
        )
    )
    db.add(
        IdempotencyKey(
            key="b",
            scope=IdempotencyScope.TOOL_CALL,
            resource_type="git.commit",
            status=IdempotencyStatus.COMPLETED,
        )
    )
    db.commit()
    assert db.query(IdempotencyKey).count() == 2

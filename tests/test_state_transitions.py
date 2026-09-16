"""Invalid state values must never silently persist — Section 26 / 8.

Two layers are tested: the SQLAlchemy Enum(validate_strings=True) layer
(catches invalid values assigned through the ORM) and the underlying
CHECK constraint (catches raw SQL that bypasses the ORM entirely — the
same defense-in-depth reasoning as PRAGMA foreign_keys).
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError

from app.db.enums import AgentRunStatus, ApprovalScope, ApprovalStatus, TaskRunStatus
from app.models.governance import Approval
from app.models.tasks import AgentRun
from tests.conftest import make_agent_version, make_task, make_task_run


def test_invalid_task_run_status_rejected_by_orm_enum(db):
    task = make_task(db)
    run = make_task_run(db, task=task, status=TaskRunStatus.CREATED)
    run.status = "not_a_real_status"
    with pytest.raises(StatementError):
        db.commit()
    db.rollback()


def test_invalid_agent_run_status_rejected_by_orm_enum(db):
    task_run = make_task_run(db)
    av = make_agent_version(db)
    run = AgentRun(task_run_id=task_run.id, agent_version_id=av.id, status=AgentRunStatus.CREATED)
    db.add(run)
    db.commit()

    run.status = "also_not_real"
    with pytest.raises(StatementError):
        db.commit()
    db.rollback()


def test_invalid_approval_status_rejected_by_orm_enum(db):
    approval = Approval(
        scope=ApprovalScope.TASK_RUN,
        scope_ref_id="x",
        operation_type="pr_merge",
        action_fingerprint="a" * 64,
        status=ApprovalStatus.PENDING,
    )
    db.add(approval)
    db.commit()

    approval.status = "bogus"
    with pytest.raises(StatementError):
        db.commit()
    db.rollback()


def test_check_constraint_rejects_invalid_status_via_raw_sql(db, engine):
    """Defense-in-depth: even a raw SQL INSERT that bypasses the ORM
    entirely must be rejected by the DB-level CHECK constraint."""
    task = make_task(db)
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                """
                INSERT INTO task_runs (id, task_id, status, timeout_seconds, created_at)
                VALUES ('11111111-1111-1111-1111-111111111111', :task_id, 'totally_invalid', 5400, '2026-01-01')
                """
            ),
            {"task_id": task.id},
        )
        db.commit()
    db.rollback()

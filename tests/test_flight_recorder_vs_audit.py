"""Flight Recorder vs. security audit log separation — Section 20.5, 24.4 #4.

execution_events and audit_events are structurally separate tables with
different schemas and different query audiences; they must never be
conflated.
"""

from app.models.observability import AuditEvent, ExecutionEvent
from tests.conftest import make_org, make_task, make_task_run


def test_tables_are_structurally_distinct(engine):
    from sqlalchemy import inspect

    inspector = inspect(engine)
    exec_cols = {c["name"] for c in inspector.get_columns("execution_events")}
    audit_cols = {c["name"] for c in inspector.get_columns("audit_events")}

    # execution_events carries run/model/tool references audit_events
    # never has; audit_events carries org_id (security/compliance scope)
    # execution_events never has.
    assert "task_run_id" in exec_cols
    assert "task_run_id" not in audit_cols
    assert "org_id" in audit_cols
    assert "org_id" not in exec_cols


def test_writing_an_execution_event_does_not_create_an_audit_event(db):
    task = make_task(db)
    run = make_task_run(db, task=task)
    db.add(
        ExecutionEvent(task_id=task.id, task_run_id=run.id, sequence_number=1, event_type="agent_run.started")
    )
    db.commit()

    assert db.query(AuditEvent).count() == 0
    assert db.query(ExecutionEvent).count() == 1


def test_audit_event_independent_of_any_task_run(db):
    """Audit events (e.g. an RBAC role change) must be recordable with no
    task_run/agent_run context at all — proving the two tables are not
    secretly coupled."""
    org = make_org(db)
    db.add(AuditEvent(org_id=org.id, event_type="role_changed", target_ref="user:abc"))
    db.commit()

    assert db.query(AuditEvent).count() == 1
    assert db.query(ExecutionEvent).count() == 0

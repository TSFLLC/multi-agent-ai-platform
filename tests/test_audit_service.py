"""Audit foundation — Section I. Real writer, structurally separate from
the Flight Recorder (extends MA0's schema-level separation test)."""

from app.models.observability import AuditEvent, ExecutionEvent
from app.services.audit_service import AuditService
from app.services.flight_recorder import FlightRecorderService
from tests.conftest import make_org, make_task, make_task_run


def test_record_and_list_for_org(db):
    org = make_org(db)
    svc = AuditService(db)
    svc.record(
        org_id=org.id,
        event_type="role_changed",
        target_ref="user:abc",
        detail={"from": "member", "to": "admin"},
    )
    svc.record(org_id=org.id, event_type="secret_rotated", target_ref="secret:xyz")

    events = svc.list_for_org(org_id=org.id)
    assert len(events) == 2
    assert {e.event_type for e in events} == {"role_changed", "secret_rotated"}


def test_list_for_org_scoped_correctly(db):
    org_a = make_org(db, name="A")
    org_b = make_org(db, name="B")
    svc = AuditService(db)
    svc.record(org_id=org_a.id, event_type="e1")
    svc.record(org_id=org_b.id, event_type="e2")

    assert [e.event_type for e in svc.list_for_org(org_id=org_a.id)] == ["e1"]
    assert [e.event_type for e in svc.list_for_org(org_id=org_b.id)] == ["e2"]


def test_audit_writes_never_appear_as_flight_recorder_events(db):
    org = make_org(db)
    task = make_task(db)
    run = make_task_run(db, task=task)

    AuditService(db).record(org_id=org.id, event_type="login")
    FlightRecorderService(db).record(task_id=task.id, task_run_id=run.id, event_type="agent_run.started")

    assert db.query(AuditEvent).count() == 1
    assert db.query(ExecutionEvent).count() == 1


def test_audit_events_endpoint_is_wired(client, db):
    org = make_org(db)
    db.commit()
    AuditService(db).record(org_id=org.id, event_type="login")
    db.commit()

    resp = client.get("/audit-events", params={"org_id": org.id})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["event_type"] == "login"

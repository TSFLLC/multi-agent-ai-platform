"""Audit endpoint authorization — MA1B fix for the MA1 gap.

GET /audit-events must require org-admin authorization (users.role in
OWNER/ADMIN, matching the caller's own org_id) — no arbitrary org_id may
bypass this.
"""

from app.db.enums import OrgRole
from app.services.audit_service import AuditService
from tests.conftest import make_org


def test_audit_events_requires_auth_at_all(client, bootstrap):
    resp = client.get("/audit-events", params={"org_id": bootstrap.organization.id})
    assert resp.status_code == 401


def test_audit_events_rejects_a_different_org_id(client, db, auth_headers, bootstrap):
    """The bootstrap Owner is Owner of their own org only — supplying
    someone else's org_id must not leak that org's audit log."""
    other_org = make_org(db, name="Someone Else's Org")
    AuditService(db).record(org_id=other_org.id, event_type="secret_rotated")
    db.commit()

    resp = client.get("/audit-events", params={"org_id": other_org.id}, headers=auth_headers)
    assert resp.status_code == 403


def test_audit_events_rejects_non_admin_role(client, db, auth_headers, bootstrap):
    """Downgrading the bootstrap user's org role below OWNER/ADMIN must
    deny audit access even for their own org."""
    bootstrap.user.role = OrgRole.MEMBER
    db.commit()

    resp = client.get("/audit-events", params={"org_id": bootstrap.organization.id}, headers=auth_headers)
    assert resp.status_code == 403


def test_audit_events_allows_admin_role(client, db, auth_headers, bootstrap):
    bootstrap.user.role = OrgRole.ADMIN
    db.commit()
    AuditService(db).record(org_id=bootstrap.organization.id, event_type="role_changed")
    db.commit()

    resp = client.get("/audit-events", params={"org_id": bootstrap.organization.id}, headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_audit_events_does_not_leak_via_membership_alone(client, db, auth_headers, bootstrap):
    """A project_memberships row is not sufficient for an org-scoped
    resource — audit access is governed by users.role, not project role."""
    bootstrap.user.role = OrgRole.VIEWER
    db.commit()

    resp = client.get("/audit-events", params={"org_id": bootstrap.organization.id}, headers=auth_headers)
    assert resp.status_code == 403

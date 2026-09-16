"""Identity/project API endpoints — MA1B, Section 25.2/25.5.

API-level (not just service-level) tests, including negative tests that
prove authorization cannot be bypassed by supplying another project/org
ID — the caller must have a real project_memberships row.
"""

from app.db.enums import ProjectRole
from app.models.identity import ProjectMembership, User
from tests.conftest import make_org, make_project


def test_get_me_returns_bootstrap_owner(client, auth_headers, bootstrap):
    resp = client.get("/me", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "owner@local"
    assert body["role"] == "owner"


def test_list_organizations_returns_only_callers_org(client, auth_headers, bootstrap):
    resp = client.get("/organizations", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == bootstrap.organization.id


def test_list_projects_returns_only_member_projects(client, auth_headers, bootstrap):
    resp = client.get("/projects", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert [p["id"] for p in body] == [bootstrap.project.id]


def test_get_project_authorized(client, auth_headers, bootstrap):
    resp = client.get(f"/projects/{bootstrap.project.id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == bootstrap.project.id


def test_get_project_unauthorized_no_token(client, bootstrap):
    resp = client.get(f"/projects/{bootstrap.project.id}")
    assert resp.status_code == 401


def test_get_project_forbidden_for_non_member_project(client, db, auth_headers, bootstrap):
    """Supplying a different, real project_id the caller has no
    membership in must be denied, not silently allowed."""
    other_org = make_org(db, name="Other Org")
    other_project = make_project(db, org=other_org, name="Someone Else's Project")
    db.commit()

    resp = client.get(f"/projects/{other_project.id}", headers=auth_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_get_project_not_found_for_nonexistent_id(client, auth_headers, bootstrap):
    resp = client.get("/projects/00000000-0000-0000-0000-000000000999", headers=auth_headers)
    assert resp.status_code == 404


def test_list_project_members_authorized(client, auth_headers, bootstrap):
    resp = client.get(f"/projects/{bootstrap.project.id}/members", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["user_id"] == bootstrap.user.id
    assert body[0]["role"] == "owner"


def test_list_project_members_forbidden_for_other_project(client, db, auth_headers, bootstrap):
    other_org = make_org(db, name="Other Org 2")
    other_project = make_project(db, org=other_org, name="Other Project 2")
    db.commit()

    resp = client.get(f"/projects/{other_project.id}/members", headers=auth_headers)
    assert resp.status_code == 403


def test_create_project_in_own_org_succeeds(client, auth_headers, bootstrap):
    resp = client.post(
        "/projects",
        headers=auth_headers,
        json={"org_id": bootstrap.organization.id, "name": "New Project"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["org_id"] == bootstrap.organization.id
    assert body["name"] == "New Project"


def test_create_project_grants_creator_as_owner(client, db, auth_headers, bootstrap):
    resp = client.post(
        "/projects",
        headers=auth_headers,
        json={"org_id": bootstrap.organization.id, "name": "Owned Project"},
    )
    project_id = resp.json()["id"]

    membership = db.query(ProjectMembership).filter_by(project_id=project_id, user_id=bootstrap.user.id).one()
    assert membership.role == ProjectRole.OWNER


def test_create_project_outside_own_org_rejected(client, db, auth_headers, bootstrap):
    other_org = make_org(db, name="Not My Org")
    db.commit()

    resp = client.post(
        "/projects", headers=auth_headers, json={"org_id": other_org.id, "name": "Sneaky Project"}
    )
    assert resp.status_code == 409


def test_add_project_member_requires_admin_action(client, db, auth_headers, bootstrap):
    other_org = make_org(db, name="Member Org")
    new_user = User(org_id=other_org.id, email="newmember@x", role="member")
    db.add(new_user)
    db.commit()

    resp = client.post(
        f"/projects/{bootstrap.project.id}/members",
        headers=auth_headers,
        json={"user_id": new_user.id, "role": "member"},
    )
    assert resp.status_code == 201
    assert resp.json()["user_id"] == new_user.id
    assert resp.json()["role"] == "member"


def test_add_project_member_forbidden_without_admin_role(client, db, auth_headers, bootstrap):
    """A VIEWER on a second project cannot add members to it."""
    viewer_project = make_project(db, org=bootstrap.organization, name="Viewer Project")
    viewer_membership = ProjectMembership(
        project_id=viewer_project.id, user_id=bootstrap.user.id, role=ProjectRole.VIEWER
    )
    db.add(viewer_membership)
    other_user = User(org_id=bootstrap.organization.id, email="other@x", role="member")
    db.add(other_user)
    db.commit()

    resp = client.post(
        f"/projects/{viewer_project.id}/members",
        headers=auth_headers,
        json={"user_id": other_user.id, "role": "member"},
    )
    assert resp.status_code == 403


def test_remove_project_member(client, db, auth_headers, bootstrap):
    other_user = User(org_id=bootstrap.organization.id, email="removable@x", role="member")
    db.add(other_user)
    db.flush()
    membership = ProjectMembership(
        project_id=bootstrap.project.id, user_id=other_user.id, role=ProjectRole.MEMBER
    )
    db.add(membership)
    db.commit()

    resp = client.delete(f"/projects/{bootstrap.project.id}/members/{membership.id}", headers=auth_headers)
    assert resp.status_code == 204
    # The DELETE happened in a different (request-scoped) session — query
    # fresh rather than Session.get() on this session's identity map
    # (which, once expired, raises ObjectDeletedError instead of
    # returning None for a row it previously loaded and now finds gone).
    remaining = db.query(ProjectMembership).filter_by(id=membership.id).count()
    assert remaining == 0

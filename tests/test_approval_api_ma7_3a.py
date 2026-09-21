"""MA7.3a — approval API: GET /approvals, GET /approvals/{id},
POST /approvals/{id}/resolve.

Authentication (401), project authorization (403: no membership / VIEWER on
resolve), fingerprint (409), idempotent replay (200), conflicting decision
(409), input validation (422), immutable provenance and audit. Disposable temp
DBs only (conftest ``client``/``db``/``bootstrap`` fixtures).
"""

import pytest
from sqlalchemy import select

from app.db.enums import ApprovalScope, ApprovalStatus, ProjectRole
from app.models.governance import Approval
from app.models.identity import Project, ProjectMembership
from app.models.observability import AuditEvent
from tests.test_approval_service_ma7_3a import make_workflow_node_run, request_workflow_approval


@pytest.fixture()
def pending(db, bootstrap):
    return request_workflow_approval(db, make_workflow_node_run(db, bootstrap.project))


def _foreign_approval(db, bootstrap, name="foreign-project"):
    """An approval in a project the (single) authenticated user has no
    membership in."""
    project = Project(org_id=bootstrap.organization.id, name=name)
    db.add(project)
    db.flush()
    return request_workflow_approval(db, make_workflow_node_run(db, project, node_key=name))


def _project_with_role(db, bootstrap, role, name):
    project = Project(org_id=bootstrap.organization.id, name=name)
    db.add(project)
    db.flush()
    db.add(ProjectMembership(project_id=project.id, user_id=bootstrap.user.id, role=role))
    db.commit()
    return project


def _resolve(client, headers, approval, approve=True, fingerprint=None, notes=None):
    body = {
        "approve": approve,
        "action_fingerprint": approval.action_fingerprint if fingerprint is None else fingerprint,
    }
    if notes is not None:
        body["notes"] = notes
    return client.post(f"/approvals/{approval.id}/resolve", headers=headers, json=body)


def _row(db, approval_id):
    db.rollback()
    return db.execute(
        select(Approval).where(Approval.id == approval_id).execution_options(populate_existing=True)
    ).scalar_one()


# --- 401 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/approvals?project_id=p"),
        ("get", "/approvals/some-id"),
        ("post", "/approvals/some-id/resolve"),
    ],
)
def test_approval_routes_require_authentication(client, method, path):
    kwargs = {"json": {"approve": True, "action_fingerprint": "f"}} if method == "post" else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_unauthenticated_resolve_with_invalid_body_is_still_401_and_changes_nothing(client, db, pending):
    response = client.post(f"/approvals/{pending.id}/resolve", json={"nonsense": True})
    assert response.status_code == 401
    assert _row(db, pending.id).status == ApprovalStatus.PENDING


def test_wrong_token_is_401(client, pending):
    response = client.get(f"/approvals/{pending.id}", headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 401


# --- GET /approvals/{id} (READ) --------------------------------------------


def test_get_approval_returns_full_contract_with_provenance_fields(client, auth_headers, pending):
    response = client.get(f"/approvals/{pending.id}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == pending.id
    assert body["scope"] == "workflow_node_run"
    assert body["scope_ref_id"] == pending.scope_ref_id
    assert body["status"] == "pending"
    assert body["action_fingerprint"] == pending.action_fingerprint
    assert body["resolved_by"] is None and body["resolved_at"] is None and body["resolution_note"] is None
    assert body["bound_artifact_id"] is None and body["expires_at"] is None


def test_get_unknown_approval_is_404(client, auth_headers, bootstrap):
    response = client.get("/approvals/does-not-exist", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_get_cross_project_approval_is_403(client, db, auth_headers, bootstrap):
    foreign = _foreign_approval(db, bootstrap)
    response = client.get(f"/approvals/{foreign.id}", headers=auth_headers)
    assert response.status_code == 403
    assert foreign.action_fingerprint not in response.text


def test_viewer_can_read_an_approval(client, db, auth_headers, bootstrap):
    project = _project_with_role(db, bootstrap, ProjectRole.VIEWER, "viewer-read")
    approval = request_workflow_approval(db, make_workflow_node_run(db, project))
    assert client.get(f"/approvals/{approval.id}", headers=auth_headers).status_code == 200


def test_approval_with_unsupported_scope_is_unreachable_403(client, db, auth_headers, bootstrap):
    orphan = Approval(
        scope=ApprovalScope.TASK_RUN,
        scope_ref_id="some-task-run",
        operation_type="op",
        action_fingerprint="f" * 64,
        status=ApprovalStatus.PENDING,
    )
    db.add(orphan)
    db.commit()
    assert client.get(f"/approvals/{orphan.id}", headers=auth_headers).status_code == 403
    assert _resolve(client, auth_headers, orphan).status_code == 403
    assert _row(db, orphan.id).status == ApprovalStatus.PENDING


# --- GET /approvals (READ, project-scoped list) ----------------------------


def test_list_requires_project_id(client, auth_headers, bootstrap):
    response = client.get("/approvals", headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_list_defaults_to_pending_and_is_project_scoped(client, db, auth_headers, bootstrap, pending):
    _foreign_approval(db, bootstrap)

    response = client.get(f"/approvals?project_id={bootstrap.project.id}", headers=auth_headers)
    assert response.status_code == 200
    assert [a["id"] for a in response.json()] == [pending.id]


def test_list_status_filter_and_all(client, db, auth_headers, bootstrap, pending):
    assert _resolve(client, auth_headers, pending, approve=False).status_code == 200
    base = f"/approvals?project_id={bootstrap.project.id}"

    assert client.get(base, headers=auth_headers).json() == []
    rejected = client.get(base + "&status=rejected", headers=auth_headers).json()
    assert [a["id"] for a in rejected] == [pending.id]
    everything = client.get(base + "&status=all", headers=auth_headers).json()
    assert [a["id"] for a in everything] == [pending.id]
    assert client.get(base + "&status=bogus", headers=auth_headers).status_code == 422


def test_list_cross_project_is_403(client, db, auth_headers, bootstrap):
    other = Project(org_id=bootstrap.organization.id, name="not-mine")
    db.add(other)
    db.commit()
    response = client.get(f"/approvals?project_id={other.id}", headers=auth_headers)
    assert response.status_code == 403


def test_list_unknown_project_is_404(client, auth_headers, bootstrap):
    assert client.get("/approvals?project_id=missing", headers=auth_headers).status_code == 404


def test_list_limit_is_validated(client, auth_headers, bootstrap):
    base = f"/approvals?project_id={bootstrap.project.id}"
    assert client.get(base + "&limit=0", headers=auth_headers).status_code == 422
    assert client.get(base + "&limit=501", headers=auth_headers).status_code == 422
    assert client.get(base + "&limit=1", headers=auth_headers).status_code == 200


# --- POST /approvals/{id}/resolve: authorization ----------------------------


def test_owner_can_approve_and_response_carries_provenance(client, db, auth_headers, bootstrap, pending):
    response = _resolve(client, auth_headers, pending, approve=True, notes="ship it")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["resolved_by"] == bootstrap.user.id
    assert body["resolved_at"] is not None
    assert body["resolution_note"] == "ship it"

    row = _row(db, pending.id)
    assert (row.status, row.resolved_by, row.resolution_note) == (
        ApprovalStatus.APPROVED,
        bootstrap.user.id,
        "ship it",
    )
    audit = db.query(AuditEvent).all()
    assert [(e.event_type, e.actor_user_id, e.target_ref) for e in audit] == [
        ("approval.approved", bootstrap.user.id, pending.id)
    ]


def test_reject_via_api(client, db, auth_headers, bootstrap, pending):
    response = _resolve(client, auth_headers, pending, approve=False, notes="not yet")
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert _row(db, pending.id).status == ApprovalStatus.REJECTED
    assert [e.event_type for e in db.query(AuditEvent).all()] == ["approval.rejected"]


@pytest.mark.parametrize("role", [ProjectRole.MEMBER, ProjectRole.ADMIN, ProjectRole.OWNER])
def test_modify_capable_roles_can_resolve(client, db, auth_headers, bootstrap, role):
    project = _project_with_role(db, bootstrap, role, f"resolve-{role.value}")
    approval = request_workflow_approval(db, make_workflow_node_run(db, project))
    assert _resolve(client, auth_headers, approval).status_code == 200


def test_viewer_cannot_resolve_and_nothing_changes(client, db, auth_headers, bootstrap):
    project = _project_with_role(db, bootstrap, ProjectRole.VIEWER, "viewer-resolve")
    approval = request_workflow_approval(db, make_workflow_node_run(db, project))

    response = _resolve(client, auth_headers, approval)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    row = _row(db, approval.id)
    assert row.status == ApprovalStatus.PENDING and row.resolved_by is None and row.resolved_at is None
    assert db.query(AuditEvent).count() == 0


def test_cross_project_user_cannot_resolve_and_nothing_changes(client, db, auth_headers, bootstrap):
    foreign = _foreign_approval(db, bootstrap, "foreign-resolve")
    response = _resolve(client, auth_headers, foreign)
    assert response.status_code == 403
    row = _row(db, foreign.id)
    assert row.status == ApprovalStatus.PENDING and row.resolved_by is None
    assert db.query(AuditEvent).count() == 0


def test_resolve_unknown_approval_is_404(client, auth_headers, bootstrap):
    response = client.post(
        "/approvals/nope/resolve", headers=auth_headers, json={"approve": True, "action_fingerprint": "f"}
    )
    assert response.status_code == 404


# --- fingerprint -----------------------------------------------------------


def test_fingerprint_mismatch_is_409_with_expected_fingerprint_and_no_change(
    client, db, auth_headers, bootstrap, pending
):
    response = _resolve(client, auth_headers, pending, fingerprint="0" * 64)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "fingerprint_mismatch"
    assert error["detail"] == {"expected_action_fingerprint": pending.action_fingerprint}

    row = _row(db, pending.id)
    assert row.status == ApprovalStatus.PENDING and row.resolved_by is None
    assert db.query(AuditEvent).count() == 0


def test_matching_fingerprint_is_required_even_for_reject(client, auth_headers, pending):
    assert _resolve(client, auth_headers, pending, approve=False, fingerprint="stale").status_code == 409


# --- idempotency / conflict -------------------------------------------------


def test_same_decision_replay_is_200_and_keeps_original_provenance(client, db, auth_headers, pending):
    first = _resolve(client, auth_headers, pending, approve=True, notes="original").json()
    again = _resolve(client, auth_headers, pending, approve=True, notes="replay must not overwrite")
    assert again.status_code == 200
    assert again.json() == first
    assert db.query(AuditEvent).count() == 1


def test_reject_twice_is_idempotent(client, auth_headers, pending):
    assert _resolve(client, auth_headers, pending, approve=False).status_code == 200
    assert _resolve(client, auth_headers, pending, approve=False).status_code == 200


@pytest.mark.parametrize("first_approve", [True, False])
def test_opposite_decision_is_409_and_provenance_is_immutable(
    client, db, auth_headers, bootstrap, pending, first_approve
):
    first = _resolve(client, auth_headers, pending, approve=first_approve, notes="original").json()

    flip = _resolve(client, auth_headers, pending, approve=not first_approve, notes="flip")
    assert flip.status_code == 409
    assert flip.json()["error"]["code"] == "invalid_state_transition"
    assert flip.json()["error"]["detail"]["status"] == first["status"]

    current = client.get(f"/approvals/{pending.id}", headers=auth_headers).json()
    assert current == first
    assert db.query(AuditEvent).count() == 1


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"approve": True},
        {"action_fingerprint": "f"},
        {"approve": "maybe", "action_fingerprint": "f"},
        {"approve": True, "action_fingerprint": ""},
        {"approve": True, "action_fingerprint": "f" * 129},
        {"approve": True, "action_fingerprint": "f", "notes": "n" * 4001},
    ],
)
def test_invalid_resolve_bodies_are_422_and_change_nothing(client, db, auth_headers, pending, body):
    response = client.post(f"/approvals/{pending.id}/resolve", headers=auth_headers, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert _row(db, pending.id).status == ApprovalStatus.PENDING


# --- contract ----------------------------------------------------------------


def test_openapi_declares_the_three_approval_operations(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "get" in paths["/approvals"]
    assert "get" in paths["/approvals/{approval_id}"]
    assert "post" in paths["/approvals/{approval_id}/resolve"]
    assert "409" in paths["/approvals/{approval_id}/resolve"]["post"]["responses"]

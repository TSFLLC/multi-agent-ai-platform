"""Evaluation Definition Registry API — authorization + lifecycle, MA6
Slice 1. Mirrors tests/test_agent_api_authorization.py's shape exactly:
every real endpoint requires authentication and project authorization --
project isolation, cross-project denial, and unauthorized access must
hold at the HTTP layer, not just the service layer.
"""

from app.db.enums import ProjectRole
from app.models.identity import ProjectMembership
from tests.conftest import make_project


def _criteria(*keys):
    return [{"key": k, "label": k.title()} for k in keys]


# -- auth / authorization ------------------------------------------------------


def test_create_definition_requires_auth(client, bootstrap):
    resp = client.post(
        "/evaluation-definitions", json={"project_id": bootstrap.project.id, "name": "X"}
    )
    assert resp.status_code == 401


def test_create_definition_authorized(client, auth_headers, bootstrap):
    resp = client.post(
        "/evaluation-definitions",
        headers=auth_headers,
        json={"project_id": bootstrap.project.id, "name": "New Rubric"},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "New Rubric"
    assert resp.json()["current_status"] is None


def test_create_definition_in_unauthorized_project_denied(client, db, auth_headers, bootstrap):
    other_project = make_project(db, name="Not Mine")
    db.commit()

    resp = client.post(
        "/evaluation-definitions",
        headers=auth_headers,
        json={"project_id": other_project.id, "name": "Sneaky Rubric"},
    )
    assert resp.status_code == 403


def test_list_definitions_requires_project_id_and_membership(client, auth_headers, bootstrap):
    resp = client.get(
        "/evaluation-definitions", headers=auth_headers, params={"project_id": bootstrap.project.id}
    )
    assert resp.status_code == 200


def test_list_definitions_cross_project_denied(client, db, auth_headers, bootstrap):
    other_project = make_project(db, name="Other")
    db.commit()

    resp = client.get(
        "/evaluation-definitions", headers=auth_headers, params={"project_id": other_project.id}
    )
    assert resp.status_code == 403


def test_get_definition_unauthorized_no_token(client, auth_headers, bootstrap):
    create = client.post(
        "/evaluation-definitions", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "X"}
    )
    definition_id = create.json()["id"]

    resp = client.get(f"/evaluation-definitions/{definition_id}")
    assert resp.status_code == 401


def test_get_definition_cross_project_denied(client, db, auth_headers, bootstrap):
    other_project = make_project(db, name="Other 2")
    from app.models.evaluation_definitions import EvaluationDefinition

    other_definition = EvaluationDefinition(project_id=other_project.id, name="Other's Rubric")
    db.add(other_definition)
    db.commit()

    resp = client.get(f"/evaluation-definitions/{other_definition.id}", headers=auth_headers)
    assert resp.status_code == 403


def test_get_definition_not_found_returns_404(client, auth_headers, bootstrap):
    resp = client.get("/evaluation-definitions/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404


# -- version lifecycle via API -------------------------------------------------


def test_full_lifecycle_via_api(client, auth_headers, bootstrap):
    create = client.post(
        "/evaluation-definitions", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "Lifecycle"}
    )
    definition_id = create.json()["id"]

    version_resp = client.post(
        f"/evaluation-definitions/{definition_id}/versions",
        headers=auth_headers,
        json={"criteria": _criteria("correctness", "coverage")},
    )
    assert version_resp.status_code == 201
    body = version_resp.json()
    assert body["status"] == "draft"
    assert body["version"] == 1
    assert [c["key"] for c in body["criteria"]] == ["correctness", "coverage"]

    publish_resp = client.post(
        f"/evaluation-definitions/{definition_id}/versions/1/publish", headers=auth_headers
    )
    assert publish_resp.status_code == 200
    assert publish_resp.json()["status"] == "active"
    assert publish_resp.json()["published_at"] is not None

    # parent definition's denormalized current_status reflects the publish
    get_resp = client.get(f"/evaluation-definitions/{definition_id}", headers=auth_headers)
    assert get_resp.json()["current_status"] == "active"

    deprecate_resp = client.post(
        f"/evaluation-definitions/{definition_id}/versions/1/deprecate", headers=auth_headers
    )
    assert deprecate_resp.status_code == 200
    assert deprecate_resp.json()["status"] == "deprecated"

    retire_resp = client.post(
        f"/evaluation-definitions/{definition_id}/versions/1/retire", headers=auth_headers
    )
    assert retire_resp.status_code == 200
    assert retire_resp.json()["status"] == "retired"


def test_publish_invalid_transition_returns_409(client, auth_headers, bootstrap):
    create = client.post(
        "/evaluation-definitions", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "X"}
    )
    definition_id = create.json()["id"]
    client.post(
        f"/evaluation-definitions/{definition_id}/versions",
        headers=auth_headers,
        json={"criteria": _criteria("a")},
    )
    client.post(f"/evaluation-definitions/{definition_id}/versions/1/publish", headers=auth_headers)

    resp = client.post(f"/evaluation-definitions/{definition_id}/versions/1/publish", headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "invalid_state_transition"


def test_publish_requires_admin_role_not_just_member(client, db, auth_headers, bootstrap):
    """A MEMBER (not ADMIN/OWNER) on a project can create but not publish
    -- publish is a consequential/administrative action, same rule as the
    Agent Registry."""
    member_project = make_project(db, org=bootstrap.organization, name="Member Project")
    db.add(ProjectMembership(project_id=member_project.id, user_id=bootstrap.user.id, role=ProjectRole.MEMBER))
    db.commit()

    create = client.post(
        "/evaluation-definitions", headers=auth_headers, json={"project_id": member_project.id, "name": "X"}
    )
    assert create.status_code == 201
    definition_id = create.json()["id"]
    client.post(
        f"/evaluation-definitions/{definition_id}/versions",
        headers=auth_headers,
        json={"criteria": _criteria("a")},
    )

    resp = client.post(f"/evaluation-definitions/{definition_id}/versions/1/publish", headers=auth_headers)
    assert resp.status_code == 403


def test_version_with_no_criteria_rejected(client, auth_headers, bootstrap):
    create = client.post(
        "/evaluation-definitions", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "X"}
    )
    definition_id = create.json()["id"]

    resp = client.post(
        f"/evaluation-definitions/{definition_id}/versions", headers=auth_headers, json={"criteria": []}
    )
    assert resp.status_code == 422


def test_version_with_duplicate_criterion_keys_rejected(client, auth_headers, bootstrap):
    create = client.post(
        "/evaluation-definitions", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "X"}
    )
    definition_id = create.json()["id"]

    resp = client.post(
        f"/evaluation-definitions/{definition_id}/versions",
        headers=auth_headers,
        json={"criteria": [{"key": "a", "label": "A1"}, {"key": "a", "label": "A2"}]},
    )
    assert resp.status_code == 422


def test_list_versions_via_api(client, auth_headers, bootstrap):
    create = client.post(
        "/evaluation-definitions", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "X"}
    )
    definition_id = create.json()["id"]
    client.post(
        f"/evaluation-definitions/{definition_id}/versions",
        headers=auth_headers,
        json={"criteria": _criteria("a")},
    )
    client.post(
        f"/evaluation-definitions/{definition_id}/versions",
        headers=auth_headers,
        json={"criteria": _criteria("a", "b")},
    )

    resp = client.get(f"/evaluation-definitions/{definition_id}/versions", headers=auth_headers)
    assert resp.status_code == 200
    versions = resp.json()
    assert [v["version"] for v in versions] == [1, 2]
    assert len(versions[1]["criteria"]) == 2


def test_method_hint_is_optional_and_never_enforced(client, auth_headers, bootstrap):
    """Slice 1 stores method_hint as a free-text, unenforced hint only --
    any string (or none) is accepted, nothing dispatches on it yet."""
    create = client.post(
        "/evaluation-definitions", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "X"}
    )
    definition_id = create.json()["id"]

    resp = client.post(
        f"/evaluation-definitions/{definition_id}/versions",
        headers=auth_headers,
        json={
            "criteria": [
                {"key": "a", "label": "A"},  # no method_hint
                {"key": "b", "label": "B", "method_hint": "deterministic"},
                {"key": "c", "label": "C", "method_hint": "some_future_method_nobody_validates_yet"},
            ]
        },
    )
    assert resp.status_code == 201
    hints = [c["method_hint"] for c in resp.json()["criteria"]]
    assert hints == [None, "deterministic", "some_future_method_nobody_validates_yet"]

"""Agent Registry API — authorization, MA2.

Every real Agent endpoint requires authentication and project
authorization — project isolation, cross-project denial, and unauthorized
access must hold at the HTTP layer, not just the service layer.
"""

from app.db.enums import ProjectRole
from app.models.identity import ProjectMembership
from tests.conftest import make_project


def test_create_agent_requires_auth(client, bootstrap):
    resp = client.post("/agents", json={"project_id": bootstrap.project.id, "name": "X", "role": "x"})
    assert resp.status_code == 401


def test_create_agent_authorized(client, auth_headers, bootstrap):
    resp = client.post(
        "/agents",
        headers=auth_headers,
        json={"project_id": bootstrap.project.id, "name": "New Agent", "role": "engineer"},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "New Agent"


def test_create_agent_in_unauthorized_project_denied(client, db, auth_headers, bootstrap):
    other_project = make_project(db, name="Not Mine")
    db.commit()

    resp = client.post(
        "/agents",
        headers=auth_headers,
        json={"project_id": other_project.id, "name": "Sneaky Agent", "role": "x"},
    )
    assert resp.status_code == 403


def test_list_agents_requires_project_id_and_membership(client, auth_headers, bootstrap):
    resp = client.get("/agents", headers=auth_headers, params={"project_id": bootstrap.project.id})
    assert resp.status_code == 200


def test_list_agents_cross_project_denied(client, db, auth_headers, bootstrap):
    other_project = make_project(db, name="Other")
    db.commit()

    resp = client.get("/agents", headers=auth_headers, params={"project_id": other_project.id})
    assert resp.status_code == 403


def test_get_agent_unauthorized_no_token(client, db, auth_headers, bootstrap):
    create = client.post(
        "/agents", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "X", "role": "x"}
    )
    agent_id = create.json()["id"]

    resp = client.get(f"/agents/{agent_id}")
    assert resp.status_code == 401


def test_get_agent_cross_project_denied(client, db, auth_headers, bootstrap):
    """An agent that exists but belongs to a project the caller has no
    membership in — must be denied, not silently returned."""
    other_project = make_project(db, name="Other 2")
    from app.models.agents import Agent

    other_agent = Agent(project_id=other_project.id, name="Other's Agent", role="x")
    db.add(other_agent)
    db.commit()

    resp = client.get(f"/agents/{other_agent.id}", headers=auth_headers)
    assert resp.status_code == 403


def test_full_lifecycle_via_api(client, auth_headers, bootstrap):
    create = client.post(
        "/agents",
        headers=auth_headers,
        json={"project_id": bootstrap.project.id, "name": "Lifecycle", "role": "x"},
    )
    agent_id = create.json()["id"]

    version_resp = client.post(
        f"/agents/{agent_id}/versions", headers=auth_headers, json={"name": "Lifecycle", "role": "x"}
    )
    assert version_resp.status_code == 201
    assert version_resp.json()["status"] == "draft"

    publish_resp = client.post(f"/agents/{agent_id}/versions/1/publish", headers=auth_headers)
    assert publish_resp.status_code == 200
    assert publish_resp.json()["status"] == "active"

    deprecate_resp = client.post(f"/agents/{agent_id}/versions/1/deprecate", headers=auth_headers)
    assert deprecate_resp.status_code == 200
    assert deprecate_resp.json()["status"] == "deprecated"

    retire_resp = client.post(f"/agents/{agent_id}/versions/1/retire", headers=auth_headers)
    assert retire_resp.status_code == 200
    assert retire_resp.json()["status"] == "retired"


def test_publish_invalid_transition_returns_409(client, auth_headers, bootstrap):
    create = client.post(
        "/agents", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "X", "role": "x"}
    )
    agent_id = create.json()["id"]
    client.post(f"/agents/{agent_id}/versions", headers=auth_headers, json={"name": "X", "role": "x"})
    client.post(f"/agents/{agent_id}/versions/1/publish", headers=auth_headers)

    resp = client.post(f"/agents/{agent_id}/versions/1/publish", headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "invalid_state_transition"


def test_publish_requires_admin_role_not_just_member(client, db, auth_headers, bootstrap):
    """A MEMBER (not ADMIN/OWNER) on a project can create but not publish
    — publish is a consequential/administrative action."""
    member_project = make_project(db, org=bootstrap.organization, name="Member Project")
    db.add(
        ProjectMembership(project_id=member_project.id, user_id=bootstrap.user.id, role=ProjectRole.MEMBER)
    )
    db.commit()

    create = client.post(
        "/agents", headers=auth_headers, json={"project_id": member_project.id, "name": "X", "role": "x"}
    )
    assert create.status_code == 201
    agent_id = create.json()["id"]
    client.post(f"/agents/{agent_id}/versions", headers=auth_headers, json={"name": "X", "role": "x"})

    resp = client.post(f"/agents/{agent_id}/versions/1/publish", headers=auth_headers)
    assert resp.status_code == 403


def test_prompt_version_via_api(client, auth_headers, bootstrap):
    create = client.post(
        "/agents", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "X", "role": "x"}
    )
    agent_id = create.json()["id"]

    resp = client.post(
        f"/agents/{agent_id}/prompt-versions", headers=auth_headers, json={"content": "You are X."}
    )
    assert resp.status_code == 201
    assert resp.json()["version"] == 1

    list_resp = client.get(f"/agents/{agent_id}/prompt-versions", headers=auth_headers)
    assert len(list_resp.json()) == 1

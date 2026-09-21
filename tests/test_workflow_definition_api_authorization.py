"""MA7.1 workflow definition/version/node/edge/graph API authentication and
project authorization -- closes the gap left open by the MA7.2 execution
authorization fix (tests/test_workflow_execution_api_authorization.py),
which covered only the /workflow-runs and .../runs endpoints."""

import pytest

from app.db.enums import ProjectRole, WorkflowNodeType
from app.models.identity import Project, ProjectMembership
from app.models.workflow import Workflow, WorkflowEdge, WorkflowNode, WorkflowVersion
from app.services.workflow_definition_service import WorkflowDefinitionService
from tests.conftest import make_agent, make_runnable_agent_version


def _draft_workflow_with_node(db, project, label="def"):
    """A DRAFT workflow version with one AGENT node -- enough to exercise
    add_edge/delete_node/delete_edge/publish without a full DAG."""
    agent = make_agent(db, project, f"{label}-agent")
    agent_version = make_runnable_agent_version(db, agent)
    service = WorkflowDefinitionService(db)
    workflow = service.create_workflow(project.id, f"{label}-workflow")
    version = service.get_latest_version(workflow.id)
    node = service.add_node(
        workflow.id, version.version, "solo", WorkflowNodeType.AGENT,
        config={"agent_version_id": agent_version.id},
    )
    terminal = service.add_node(workflow.id, version.version, "result", WorkflowNodeType.TERMINAL)
    return workflow, version, node, terminal


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/workflows", {"params": {"project_id": "p"}}),
        ("post", "/workflows", {"json": {"project_id": "p", "name": "n"}}),
        ("get", "/workflows/w", {}),
        ("post", "/workflows/w/versions", {}),
        ("post", "/workflows/w/versions/1/publish", {}),
        ("get", "/workflows/w/versions/1", {}),
        ("get", "/workflows/w/versions", {}),
        ("post", "/workflows/w/versions/1/nodes", {"json": {"node_key": "k", "node_type": "agent"}}),
        ("delete", "/workflows/w/versions/1/nodes/n", {}),
        ("post", "/workflows/w/versions/1/edges", {"params": {"from_node_id": "a", "to_node_id": "b"}}),
        ("delete", "/workflows/w/versions/1/edges/e", {}),
        ("get", "/workflows/w/versions/1/graph", {}),
    ],
)
def test_definition_routes_require_authentication(client, method, path, kwargs):
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401, f"{method.upper()} {path} -> {response.status_code}"


def test_cross_project_reads_blocked(client, db, auth_headers, bootstrap):
    other_project = Project(org_id=bootstrap.organization.id, name="def-unshared-read")
    db.add(other_project)
    db.flush()
    workflow, version, _node, _terminal = _draft_workflow_with_node(db, other_project, "read")

    assert client.get(
        "/workflows", params={"project_id": other_project.id}, headers=auth_headers
    ).status_code == 403

    assert client.get(f"/workflows/{workflow.id}", headers=auth_headers).status_code == 403
    assert client.get(f"/workflows/{workflow.id}/versions", headers=auth_headers).status_code == 403
    assert client.get(
        f"/workflows/{workflow.id}/versions/{version.version}", headers=auth_headers
    ).status_code == 403
    assert client.get(
        f"/workflows/{workflow.id}/versions/{version.version}/graph", headers=auth_headers
    ).status_code == 403


def test_cross_project_mutations_blocked_with_zero_side_effects(client, db, auth_headers, bootstrap):
    other_project = Project(org_id=bootstrap.organization.id, name="def-unshared-write")
    db.add(other_project)
    db.flush()
    workflow, version, node, terminal = _draft_workflow_with_node(db, other_project, "write")

    workflow_count = db.query(Workflow).count()
    version_count = db.query(WorkflowVersion).count()
    node_count = db.query(WorkflowNode).count()
    edge_count = db.query(WorkflowEdge).count()

    # create_workflow into a project the caller has no membership in
    create = client.post(
        "/workflows", headers=auth_headers, json={"project_id": other_project.id, "name": "hostile"}
    )
    assert create.status_code == 403

    # create_workflow_version
    add_version = client.post(f"/workflows/{workflow.id}/versions", headers=auth_headers)
    assert add_version.status_code == 403

    # add_workflow_node
    add_node = client.post(
        f"/workflows/{workflow.id}/versions/{version.version}/nodes",
        headers=auth_headers,
        json={"node_key": "hostile", "node_type": "terminal"},
    )
    assert add_node.status_code == 403

    # add_workflow_edge
    add_edge = client.post(
        f"/workflows/{workflow.id}/versions/{version.version}/edges",
        headers=auth_headers,
        params={"from_node_id": node.id, "to_node_id": terminal.id},
    )
    assert add_edge.status_code == 403

    # delete_workflow_node / delete_workflow_edge
    delete_node = client.delete(
        f"/workflows/{workflow.id}/versions/{version.version}/nodes/{node.id}", headers=auth_headers
    )
    assert delete_node.status_code == 403

    delete_edge = client.delete(
        f"/workflows/{workflow.id}/versions/{version.version}/edges/nonexistent", headers=auth_headers
    )
    assert delete_edge.status_code == 403

    # publish_workflow_version
    publish = client.post(
        f"/workflows/{workflow.id}/versions/{version.version}/publish", headers=auth_headers
    )
    assert publish.status_code == 403

    db.expire_all()
    assert db.query(Workflow).count() == workflow_count
    assert db.query(WorkflowVersion).count() == version_count
    assert db.query(WorkflowNode).count() == node_count
    assert db.query(WorkflowEdge).count() == edge_count
    db.refresh(version)
    assert version.status.value == "draft"


def test_read_only_project_member_can_read_but_not_modify(client, db, auth_headers, bootstrap):
    viewer_project = Project(org_id=bootstrap.organization.id, name="def-viewer-project")
    db.add(viewer_project)
    db.flush()
    db.add(ProjectMembership(project_id=viewer_project.id, user_id=bootstrap.user.id, role=ProjectRole.VIEWER))
    db.commit()

    workflow, version, node, terminal = _draft_workflow_with_node(db, viewer_project, "viewer")

    # READ user can perform reads
    assert client.get(f"/workflows/{workflow.id}", headers=auth_headers).status_code == 200
    assert client.get(f"/workflows/{workflow.id}/versions", headers=auth_headers).status_code == 200
    assert client.get(
        f"/workflows/{workflow.id}/versions/{version.version}", headers=auth_headers
    ).status_code == 200
    assert client.get(
        f"/workflows/{workflow.id}/versions/{version.version}/graph", headers=auth_headers
    ).status_code == 200
    assert client.get("/workflows", params={"project_id": viewer_project.id}, headers=auth_headers).status_code == 200

    node_count = db.query(WorkflowNode).count()
    edge_count = db.query(WorkflowEdge).count()
    version_count = db.query(WorkflowVersion).count()

    # READ user cannot modify
    assert client.post(f"/workflows/{workflow.id}/versions", headers=auth_headers).status_code == 403
    assert client.post(
        f"/workflows/{workflow.id}/versions/{version.version}/nodes",
        headers=auth_headers,
        json={"node_key": "hostile", "node_type": "terminal"},
    ).status_code == 403
    assert client.post(
        f"/workflows/{workflow.id}/versions/{version.version}/edges",
        headers=auth_headers,
        params={"from_node_id": node.id, "to_node_id": terminal.id},
    ).status_code == 403
    assert client.delete(
        f"/workflows/{workflow.id}/versions/{version.version}/nodes/{node.id}", headers=auth_headers
    ).status_code == 403
    assert client.delete(
        f"/workflows/{workflow.id}/versions/{version.version}/edges/nonexistent", headers=auth_headers
    ).status_code == 403
    assert client.post(
        f"/workflows/{workflow.id}/versions/{version.version}/publish", headers=auth_headers
    ).status_code == 403
    assert client.post(
        "/workflows", headers=auth_headers, json={"project_id": viewer_project.id, "name": "hostile"}
    ).status_code == 403

    db.expire_all()
    assert db.query(WorkflowNode).count() == node_count
    assert db.query(WorkflowEdge).count() == edge_count
    assert db.query(WorkflowVersion).count() == version_count


def test_modify_project_member_can_perform_full_definition_lifecycle(client, db, auth_headers, bootstrap):
    member_project = Project(org_id=bootstrap.organization.id, name="def-member-project")
    db.add(member_project)
    db.flush()
    db.add(ProjectMembership(project_id=member_project.id, user_id=bootstrap.user.id, role=ProjectRole.MEMBER))
    db.commit()

    agent = make_agent(db, member_project, "member-agent")
    agent_version = make_runnable_agent_version(db, agent)
    db.commit()

    create = client.post(
        "/workflows", headers=auth_headers, json={"project_id": member_project.id, "name": "member-workflow"}
    )
    assert create.status_code == 201
    workflow_id = create.json()["id"]

    versions = client.get(f"/workflows/{workflow_id}/versions", headers=auth_headers)
    assert versions.status_code == 200
    version_number = versions.json()[0]["version"]

    node1 = client.post(
        f"/workflows/{workflow_id}/versions/{version_number}/nodes",
        headers=auth_headers,
        json={"node_key": "solo", "node_type": "agent", "config": {"agent_version_id": agent_version.id}},
    )
    assert node1.status_code == 201
    node2 = client.post(
        f"/workflows/{workflow_id}/versions/{version_number}/nodes",
        headers=auth_headers,
        json={"node_key": "result", "node_type": "terminal"},
    )
    assert node2.status_code == 201

    edge = client.post(
        f"/workflows/{workflow_id}/versions/{version_number}/edges",
        headers=auth_headers,
        params={"from_node_id": node1.json()["id"], "to_node_id": node2.json()["id"]},
    )
    assert edge.status_code == 201

    graph = client.get(f"/workflows/{workflow_id}/versions/{version_number}/graph", headers=auth_headers)
    assert graph.status_code == 200
    assert len(graph.json()["nodes"]) == 2
    assert len(graph.json()["edges"]) == 1

    publish = client.post(
        f"/workflows/{workflow_id}/versions/{version_number}/publish", headers=auth_headers
    )
    assert publish.status_code == 200
    assert publish.json()["status"] == "active"

    # Preserve existing authorized behavior: a second DRAFT version can
    # still be created, edited and its edge/node removed.
    new_version = client.post(f"/workflows/{workflow_id}/versions", headers=auth_headers)
    assert new_version.status_code == 201
    v2 = new_version.json()["version"]

    v2_node1 = client.post(
        f"/workflows/{workflow_id}/versions/{v2}/nodes",
        headers=auth_headers,
        json={"node_key": "solo2", "node_type": "agent", "config": {"agent_version_id": agent_version.id}},
    )
    v2_node2 = client.post(
        f"/workflows/{workflow_id}/versions/{v2}/nodes",
        headers=auth_headers,
        json={"node_key": "result2", "node_type": "terminal"},
    )
    assert v2_node1.status_code == 201 and v2_node2.status_code == 201

    v2_edge = client.post(
        f"/workflows/{workflow_id}/versions/{v2}/edges",
        headers=auth_headers,
        params={"from_node_id": v2_node1.json()["id"], "to_node_id": v2_node2.json()["id"]},
    )
    assert v2_edge.status_code == 201

    delete_edge = client.delete(
        f"/workflows/{workflow_id}/versions/{v2}/edges/{v2_edge.json()['id']}", headers=auth_headers
    )
    assert delete_edge.status_code == 200

    delete_node = client.delete(
        f"/workflows/{workflow_id}/versions/{v2}/nodes/{v2_node2.json()['id']}", headers=auth_headers
    )
    assert delete_node.status_code == 200

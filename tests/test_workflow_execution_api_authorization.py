"""MA7.2 workflow execution API authentication and project authorization."""

from datetime import datetime, timezone

import pytest

from app.db.enums import ExecutionMode, ProjectRole, TaskRunStatus, WorkflowNodeType
from app.models.identity import Project, ProjectMembership
from app.models.tasks import TaskRun
from app.models.workflow import WorkflowNodeRun, WorkflowRun
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.workflow_execution_service import WorkflowExecutionService
from tests.conftest import make_agent, make_runnable_agent_version, make_task


def _active_workflow(db, project, label="api"):
    planner = make_agent(db, project, f"{label}-planner")
    planner_version = make_runnable_agent_version(db, planner)
    engineer = make_agent(db, project, f"{label}-engineer")
    engineer_version = make_runnable_agent_version(db, engineer)
    service = WorkflowDefinitionService(db)
    workflow = service.create_workflow(project.id, f"{label}-workflow")
    version = service.get_latest_version(workflow.id)
    node1 = service.add_node(
        workflow.id, version.version, "planner", WorkflowNodeType.AGENT,
        config={"agent_version_id": planner_version.id},
    )
    node2 = service.add_node(
        workflow.id, version.version, "engineer", WorkflowNodeType.AGENT,
        config={"agent_version_id": engineer_version.id},
    )
    terminal = service.add_node(workflow.id, version.version, "result", WorkflowNodeType.TERMINAL)
    service.add_edge(workflow.id, version.version, node1.id, node2.id)
    service.add_edge(workflow.id, version.version, node2.id, terminal.id)
    published = service.publish_version(workflow.id, version.version)
    task = make_task(db, project, execution_mode=ExecutionMode.SINGLE_AGENT)
    task_run = TaskRun(task_id=task.id, status=TaskRunStatus.CREATED, created_at=datetime.now(timezone.utc))
    db.add(task_run)
    db.commit()
    return workflow, published, task_run


def _started_run(db, project, label="run"):
    workflow, version, task_run = _active_workflow(db, project, label)
    run = WorkflowExecutionService(db).start_workflow_run(version.id, task_run.id)
    return workflow, version, task_run, run


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/workflows/w/versions/1/runs"),
        ("get", "/workflow-runs/r"),
        ("get", "/workflow-runs/r/nodes"),
        ("post", "/workflow-runs/r/cancel"),
    ],
)
def test_workflow_execution_routes_require_authentication(client, method, path):
    kwargs = {"json": {"task_run_id": "t"}} if method == "post" and path.endswith("runs") else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401


def test_unauthenticated_attempts_placeholder_is_rejected_before_501(client):
    response = client.get("/workflow-runs/r/nodes/n/attempts")
    assert response.status_code == 401


def test_cross_project_user_cannot_read_list_or_cancel(client, db, auth_headers, bootstrap):
    other_project = Project(org_id=bootstrap.organization.id, name="unshared-project")
    db.add(other_project)
    db.flush()
    _, version, _, run = _started_run(db, other_project, "unshared")
    before = (run.status, run.cancellation_requested_at, db.query(WorkflowNodeRun).count())

    assert client.get(f"/workflow-runs/{run.id}", headers=auth_headers).status_code == 403
    assert client.get(f"/workflow-runs/{run.id}/nodes", headers=auth_headers).status_code == 403
    assert client.get(
        f"/workflows/{run.workflow_version_id}/versions/1/runs".replace(run.workflow_version_id, version.workflow_id),
        headers=auth_headers,
    ).status_code == 403
    assert client.post(f"/workflow-runs/{run.id}/cancel", headers=auth_headers).status_code == 403

    db.refresh(run)
    assert (run.status, run.cancellation_requested_at, db.query(WorkflowNodeRun).count()) == before


def test_cross_project_user_cannot_start_and_start_has_no_side_effects(client, db, auth_headers, bootstrap):
    other_project = Project(org_id=bootstrap.organization.id, name="unshared-start")
    db.add(other_project)
    db.flush()
    _, version, task_run = _active_workflow(db, other_project, "unshared-start")
    before = (db.query(WorkflowRun).count(), db.query(WorkflowNodeRun).count(), db.query(TaskRun).count())

    response = client.post(
        f"/workflows/{version.workflow_id}/versions/{version.version}/runs",
        headers=auth_headers,
        json={"task_run_id": task_run.id},
    )
    assert response.status_code == 403
    assert (db.query(WorkflowRun).count(), db.query(WorkflowNodeRun).count(), db.query(TaskRun).count()) == before


def test_authorized_read_and_modify_operations(client, db, auth_headers, bootstrap):
    _, version, _task_run, run = _started_run(db, bootstrap.project, "authorized")

    assert client.get(f"/workflow-runs/{run.id}", headers=auth_headers).status_code == 200
    assert client.get(f"/workflow-runs/{run.id}/nodes", headers=auth_headers).status_code == 200
    # MA7.6B: the attempts endpoint is implemented now -- an authorized caller
    # asking about a node that does not exist in this run's version gets 404,
    # never the old MA7.2 501 placeholder.
    assert client.get(f"/workflow-runs/{run.id}/nodes/x/attempts", headers=auth_headers).status_code == 404

    # The bootstrap owner has MODIFY access and can start another same-project run.
    second_task = make_task(db, bootstrap.project, execution_mode=ExecutionMode.SINGLE_AGENT)
    second_task_run = TaskRun(task_id=second_task.id, status=TaskRunStatus.CREATED)
    db.add(second_task_run)
    db.commit()
    start = client.post(
        f"/workflows/{version.workflow_id}/versions/{version.version}/runs",
        headers=auth_headers,
        json={"task_run_id": second_task_run.id},
    )
    assert start.status_code == 201
    assert client.post(f"/workflow-runs/{run.id}/cancel", headers=auth_headers).status_code == 200


def test_read_only_project_member_cannot_start_or_cancel(client, db, auth_headers, bootstrap):
    viewer_project = Project(org_id=bootstrap.organization.id, name="viewer-project")
    db.add(viewer_project)
    db.flush()
    db.add(ProjectMembership(project_id=viewer_project.id, user_id=bootstrap.user.id, role=ProjectRole.VIEWER))
    db.commit()
    _, version, task_run, run = _started_run(db, viewer_project, "viewer")
    before = (run.status, run.cancellation_requested_at, db.query(WorkflowRun).count())

    start = client.post(
        f"/workflows/{version.workflow_id}/versions/{version.version}/runs",
        headers=auth_headers,
        json={"task_run_id": task_run.id},
    )
    cancel = client.post(f"/workflow-runs/{run.id}/cancel", headers=auth_headers)
    assert start.status_code == 403
    assert cancel.status_code == 403
    db.refresh(run)
    assert (run.status, run.cancellation_requested_at, db.query(WorkflowRun).count()) == before

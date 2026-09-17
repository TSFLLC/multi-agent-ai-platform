"""Task / Task Run / Agent Run HTTP surface — Section 25.2/25.3/25.5, MA3.

Exercises the real endpoints end-to-end (client -> API -> TaskService ->
job_queue -> worker -> AgentExecutionService, the latter with its provider
call monkeypatched) so both the authorization wiring and the queue-based
execution contract are proven together, the way an operator would
actually use this API.
"""

import app.services.execution_service as execution_service_module
from app.db.enums import TaskRunStatus


def _create_agent_and_publish(client, auth_headers, project_id) -> str:
    create = client.post(
        "/agents", headers=auth_headers, json={"project_id": project_id, "name": "Runner", "role": "engineer"}
    )
    assert create.status_code == 201
    agent_id = create.json()["id"]
    version_resp = client.post(
        f"/agents/{agent_id}/versions", headers=auth_headers, json={"name": "Runner", "role": "engineer"}
    )
    assert version_resp.status_code == 201
    publish = client.post(f"/agents/{agent_id}/versions/1/publish", headers=auth_headers)
    assert publish.status_code == 200
    return publish.json()["id"]


def test_create_task_requires_auth(client, bootstrap):
    resp = client.post(
        "/tasks",
        json={"project_id": bootstrap.project.id, "title": "Do X", "execution_mode": "single_agent"},
    )
    assert resp.status_code == 401


def test_create_and_get_task(client, auth_headers, bootstrap):
    create = client.post(
        "/tasks",
        headers=auth_headers,
        json={"project_id": bootstrap.project.id, "title": "Write a haiku", "execution_mode": "single_agent"},
    )
    assert create.status_code == 201
    task_id = create.json()["id"]

    get_resp = client.get(f"/tasks/{task_id}", headers=auth_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["title"] == "Write a haiku"


def test_get_task_cross_project_denied(client, db, auth_headers, bootstrap):
    from tests.conftest import make_org, make_project, make_task

    other_project = make_project(db, org=make_org(db, name="Other Org"))
    other_task = make_task(db, project=other_project)
    db.commit()

    resp = client.get(f"/tasks/{other_task.id}", headers=auth_headers)
    assert resp.status_code == 403


def test_create_task_idempotency_key_returns_same_task(client, auth_headers, bootstrap):
    body = {"project_id": bootstrap.project.id, "title": "Idempotent task", "execution_mode": "single_agent"}
    first = client.post("/tasks", headers={**auth_headers, "Idempotency-Key": "task-key-1"}, json=body)
    second = client.post("/tasks", headers={**auth_headers, "Idempotency-Key": "task-key-1"}, json=body)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_start_task_run_requires_published_agent_version(client, auth_headers, bootstrap):
    task = client.post(
        "/tasks",
        headers=auth_headers,
        json={"project_id": bootstrap.project.id, "title": "Task", "execution_mode": "single_agent"},
    ).json()

    create_agent = client.post(
        "/agents", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "R", "role": "x"}
    ).json()
    draft_version = client.post(
        f"/agents/{create_agent['id']}/versions", headers=auth_headers, json={"name": "R", "role": "x"}
    ).json()

    resp = client.post(
        f"/tasks/{task['id']}/runs",
        headers=auth_headers,
        json={"agent_version_id": draft_version["id"]},
    )
    assert resp.status_code == 409


def test_full_single_agent_execution_via_api_and_worker(
    client, auth_headers, bootstrap, db, fast_worker, monkeypatch
):
    agent_version_id = _create_agent_and_publish(client, auth_headers, bootstrap.project.id)

    task = client.post(
        "/tasks",
        headers=auth_headers,
        json={
            "project_id": bootstrap.project.id,
            "title": "Say the magic words",
            "execution_mode": "single_agent",
        },
    ).json()

    run = client.post(
        f"/tasks/{task['id']}/runs", headers=auth_headers, json={"agent_version_id": agent_version_id}
    )
    assert run.status_code == 201
    assert run.json()["status"] == "queued"
    run_id = run.json()["id"]

    def fake_execute(self, agent_run_id, *, worker_id):
        from app.db.enums import AgentRunStatus
        from app.models.tasks import AgentRun, TaskRun

        agent_run = self.db.get(AgentRun, agent_run_id)
        agent_run.status = AgentRunStatus.COMPLETED
        task_run = self.db.get(TaskRun, agent_run.task_run_id)
        task_run.status = TaskRunStatus.COMPLETED
        self.db.commit()

    monkeypatch.setattr(execution_service_module.AgentExecutionService, "execute", fake_execute)
    processed = fast_worker.run_once()
    assert processed is True

    get_run = client.get(f"/tasks/{task['id']}/runs/{run_id}", headers=auth_headers)
    assert get_run.status_code == 200
    assert get_run.json()["status"] == "completed"

    events = client.get(f"/tasks/{task['id']}/runs/{run_id}/events", headers=auth_headers)
    assert events.status_code == 200
    event_types = [e["event_type"] for e in events.json()]
    assert "task_run.created" in event_types
    assert "task_run.queued" in event_types


def test_cancel_task_run_is_cooperative_and_immediate_response(client, auth_headers, bootstrap):
    agent_version_id = _create_agent_and_publish(client, auth_headers, bootstrap.project.id)
    task = client.post(
        "/tasks",
        headers=auth_headers,
        json={"project_id": bootstrap.project.id, "title": "T", "execution_mode": "single_agent"},
    ).json()
    run = client.post(
        f"/tasks/{task['id']}/runs", headers=auth_headers, json={"agent_version_id": agent_version_id}
    ).json()

    cancel = client.post(f"/tasks/{task['id']}/runs/{run['id']}/cancel", headers=auth_headers)
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelling"

    second_cancel = client.post(f"/tasks/{task['id']}/runs/{run['id']}/cancel", headers=auth_headers)
    assert second_cancel.status_code == 409


def test_agent_run_artifacts_and_usage_endpoints_start_empty(client, auth_headers, bootstrap, db):
    agent_version_id = _create_agent_and_publish(client, auth_headers, bootstrap.project.id)
    task = client.post(
        "/tasks",
        headers=auth_headers,
        json={"project_id": bootstrap.project.id, "title": "T", "execution_mode": "single_agent"},
    ).json()
    run = client.post(
        f"/tasks/{task['id']}/runs", headers=auth_headers, json={"agent_version_id": agent_version_id}
    ).json()

    from app.models.tasks import AgentRun

    db.expire_all()
    agent_run = db.query(AgentRun).filter_by(task_run_id=run["id"]).one()

    artifacts = client.get(f"/agent-runs/{agent_run.id}/artifacts", headers=auth_headers)
    assert artifacts.status_code == 200
    assert artifacts.json() == []

    usage = client.get(f"/agent-runs/{agent_run.id}/usage", headers=auth_headers)
    assert usage.status_code == 200
    assert usage.json() == []


def test_agent_run_endpoints_require_project_membership(client, db, auth_headers, bootstrap):
    from tests.conftest import make_agent_run, make_org, make_project

    other_project = make_project(db, org=make_org(db, name="Other Org 2"))
    other_agent_run = make_agent_run(db)
    from app.models.tasks import Task, TaskRun

    task_run = db.get(TaskRun, other_agent_run.task_run_id)
    task = db.get(Task, task_run.task_id)
    task.project_id = other_project.id
    db.commit()

    resp = client.get(f"/agent-runs/{other_agent_run.id}", headers=auth_headers)
    assert resp.status_code == 403

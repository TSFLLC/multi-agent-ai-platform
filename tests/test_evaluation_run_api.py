"""Evaluation Run API — authorization + manual-trigger lifecycle, MA6
Slice 2, extended by Slice 3C's AGENT_EVALUATOR method on the same
single-AgentRun endpoint. Mirrors tests/test_evaluation_definition_api.py's
shape: every real endpoint requires authentication and project
authorization -- project isolation and cross-project denial must hold at
the HTTP layer, not just the service layer. No auto-enqueue-on-completion
path exists to test here on purpose (manual trigger only, MA6 V1
invariant).
"""

from app.db.enums import VersionStatus
from app.services.evaluation_definition_service import EvaluationDefinitionService
from app.services.evaluation_execution_service import EvaluationExecutionService
from tests.conftest import (
    make_agent,
    make_agent_run,
    make_agent_version,
    make_artifact_with_content,
    make_evaluation_definition,
    make_project,
    make_task,
    make_task_run,
)


def _subject(db, tmp_path, project, content="hello"):
    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task)
    agent_version = make_agent_version(db, status=VersionStatus.ACTIVE)
    agent_run = make_agent_run(db, task_run=task_run, agent_version=agent_version)
    artifact = make_artifact_with_content(db, tmp_path, agent_run=agent_run, content=content)
    db.commit()
    return agent_run, artifact


def _published_version(db, project, criteria):
    definition = make_evaluation_definition(db, project=project)
    db.commit()
    version = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id, description=None, criteria=criteria
    )
    return EvaluationDefinitionService(db).publish_version(version.id)


def _criteria(*keys):
    return [{"key": k, "label": k.title()} for k in keys]


def _evaluator_version(db, project):
    agent = make_agent(db, project=project, name="Evaluator")
    version = make_agent_version(db, agent=agent, status=VersionStatus.ACTIVE)
    db.commit()
    return version


# -- create: auth / authorization -----------------------------------------------


def test_create_evaluation_run_requires_auth(client, db, bootstrap, tmp_path):
    agent_run, artifact = _subject(db, tmp_path, bootstrap.project)
    version = _published_version(db, bootstrap.project, _criteria("non_empty_output"))

    resp = client.post(
        f"/agent-runs/{agent_run.id}/evaluations",
        json={"subject_artifact_id": artifact.id, "evaluation_definition_version_id": version.id},
    )
    assert resp.status_code == 401


def test_create_evaluation_run_authorized(client, db, auth_headers, bootstrap, tmp_path):
    agent_run, artifact = _subject(db, tmp_path, bootstrap.project)
    version = _published_version(db, bootstrap.project, _criteria("non_empty_output"))

    resp = client.post(
        f"/agent-runs/{agent_run.id}/evaluations",
        headers=auth_headers,
        json={"subject_artifact_id": artifact.id, "evaluation_definition_version_id": version.id},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["method"] == "deterministic"
    assert body["subject_artifact_content_hash"] == artifact.content_hash
    assert body["criterion_results"] == []


def test_create_evaluation_run_cross_project_denied(client, db, auth_headers, bootstrap, tmp_path):
    other_project = make_project(db, name="Other")
    agent_run, artifact = _subject(db, tmp_path, other_project)
    version = _published_version(db, other_project, _criteria("non_empty_output"))

    resp = client.post(
        f"/agent-runs/{agent_run.id}/evaluations",
        headers=auth_headers,
        json={"subject_artifact_id": artifact.id, "evaluation_definition_version_id": version.id},
    )
    assert resp.status_code == 403


# -- create: exact-artifact protection / definition-version validation ---------


def test_create_evaluation_run_wrong_artifact_rejected(client, db, auth_headers, bootstrap, tmp_path):
    agent_run, _artifact = _subject(db, tmp_path, bootstrap.project)
    _other_agent_run, other_artifact = _subject(db, tmp_path, bootstrap.project)
    version = _published_version(db, bootstrap.project, _criteria("non_empty_output"))

    resp = client.post(
        f"/agent-runs/{agent_run.id}/evaluations",
        headers=auth_headers,
        json={"subject_artifact_id": other_artifact.id, "evaluation_definition_version_id": version.id},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_create_evaluation_run_unpublished_version_rejected(client, db, auth_headers, bootstrap, tmp_path):
    agent_run, artifact = _subject(db, tmp_path, bootstrap.project)
    definition = make_evaluation_definition(db, project=bootstrap.project)
    db.commit()
    draft = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id, description=None, criteria=_criteria("a")
    )

    resp = client.post(
        f"/agent-runs/{agent_run.id}/evaluations",
        headers=auth_headers,
        json={"subject_artifact_id": artifact.id, "evaluation_definition_version_id": draft.id},
    )
    assert resp.status_code == 409


def test_create_evaluation_run_missing_agent_run_returns_404(client, db, auth_headers, bootstrap, tmp_path):
    _agent_run, artifact = _subject(db, tmp_path, bootstrap.project)
    version = _published_version(db, bootstrap.project, _criteria("non_empty_output"))

    resp = client.post(
        "/agent-runs/does-not-exist/evaluations",
        headers=auth_headers,
        json={"subject_artifact_id": artifact.id, "evaluation_definition_version_id": version.id},
    )
    assert resp.status_code == 404


# -- read endpoints ---------------------------------------------------------------


def test_get_evaluation_run_not_found_returns_404(client, auth_headers, bootstrap):
    resp = client.get("/evaluation-runs/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404


def test_list_evaluation_runs_allows_multiple_for_same_subject(client, db, auth_headers, bootstrap, tmp_path):
    agent_run, artifact = _subject(db, tmp_path, bootstrap.project)
    version = _published_version(db, bootstrap.project, _criteria("non_empty_output"))

    for _ in range(2):
        resp = client.post(
            f"/agent-runs/{agent_run.id}/evaluations",
            headers=auth_headers,
            json={"subject_artifact_id": artifact.id, "evaluation_definition_version_id": version.id},
        )
        assert resp.status_code == 201

    resp = client.get(f"/agent-runs/{agent_run.id}/evaluations", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_evaluation_run_after_worker_completes_it(
    client, db, auth_headers, bootstrap, tmp_path, fast_worker
):
    agent_run, artifact = _subject(db, tmp_path, bootstrap.project, content="real content")
    version = _published_version(db, bootstrap.project, _criteria("non_empty_output"))

    create = client.post(
        f"/agent-runs/{agent_run.id}/evaluations",
        headers=auth_headers,
        json={"subject_artifact_id": artifact.id, "evaluation_definition_version_id": version.id},
    )
    run_id = create.json()["id"]

    assert fast_worker.run_once() is True

    resp = client.get(f"/evaluation-runs/{run_id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert len(body["criterion_results"]) == 1
    assert body["criterion_results"][0]["finding"] == "met"


def test_get_evaluation_run_cross_project_denied(client, db, auth_headers, bootstrap, tmp_path):
    other_project = make_project(db, name="Isolated")
    agent_run, artifact = _subject(db, tmp_path, other_project)
    version = _published_version(db, other_project, _criteria("non_empty_output"))
    run = EvaluationExecutionService(db).create_run(
        agent_run_id=agent_run.id,
        subject_artifact_id=artifact.id,
        evaluation_definition_version_id=version.id,
    )

    resp = client.get(f"/evaluation-runs/{run.id}", headers=auth_headers)
    assert resp.status_code == 403


def test_list_evaluation_runs_cross_project_denied(client, db, auth_headers, bootstrap, tmp_path):
    other_project = make_project(db, name="Isolated 2")
    agent_run, _artifact = _subject(db, tmp_path, other_project)

    resp = client.get(f"/agent-runs/{agent_run.id}/evaluations", headers=auth_headers)
    assert resp.status_code == 403


# -- create: method=agent_evaluator (Slice 3C wiring of Slice 3B) ---------------


def test_create_agent_evaluator_run_requires_evaluator_agent_version_id(
    client, db, auth_headers, bootstrap, tmp_path
):
    agent_run, artifact = _subject(db, tmp_path, bootstrap.project)
    version = _published_version(db, bootstrap.project, _criteria("a"))

    resp = client.post(
        f"/agent-runs/{agent_run.id}/evaluations",
        headers=auth_headers,
        json={
            "subject_artifact_id": artifact.id,
            "evaluation_definition_version_id": version.id,
            "method": "agent_evaluator",
        },
    )
    assert resp.status_code == 422


def test_create_agent_evaluator_run_authorized(client, db, auth_headers, bootstrap, tmp_path):
    agent_run, artifact = _subject(db, tmp_path, bootstrap.project)
    version = _published_version(db, bootstrap.project, _criteria("a"))
    evaluator_version = _evaluator_version(db, bootstrap.project)

    resp = client.post(
        f"/agent-runs/{agent_run.id}/evaluations",
        headers=auth_headers,
        json={
            "subject_artifact_id": artifact.id,
            "evaluation_definition_version_id": version.id,
            "method": "agent_evaluator",
            "evaluator_agent_version_id": evaluator_version.id,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["method"] == "agent_evaluator"
    assert body["status"] == "running"
    assert body["evaluator_agent_version_id"] == evaluator_version.id
    assert body["evaluator_task_run_id"] is not None
    assert body["evaluator_agent_run_id"] is not None

    # provenance survives a GET round trip -- needed by 3D/UI
    get_resp = client.get(f"/evaluation-runs/{body['id']}", headers=auth_headers)
    assert get_resp.status_code == 200
    get_body = get_resp.json()
    assert get_body["evaluator_agent_version_id"] == evaluator_version.id
    assert get_body["evaluator_task_run_id"] == body["evaluator_task_run_id"]
    assert get_body["evaluator_agent_run_id"] == body["evaluator_agent_run_id"]


def test_create_agent_evaluator_run_cross_project_evaluator_version_rejected(
    client, db, auth_headers, bootstrap, tmp_path
):
    other_project = make_project(db, name="Isolated Evaluator")
    agent_run, artifact = _subject(db, tmp_path, bootstrap.project)
    version = _published_version(db, bootstrap.project, _criteria("a"))
    evaluator_version = _evaluator_version(db, other_project)

    resp = client.post(
        f"/agent-runs/{agent_run.id}/evaluations",
        headers=auth_headers,
        json={
            "subject_artifact_id": artifact.id,
            "evaluation_definition_version_id": version.id,
            "method": "agent_evaluator",
            "evaluator_agent_version_id": evaluator_version.id,
        },
    )
    assert resp.status_code == 409

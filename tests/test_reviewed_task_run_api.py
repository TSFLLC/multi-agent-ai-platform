"""Reviewed Task Run HTTP surface — MA4.

The review *state machine* (primary -> reviewer -> repair -> ACCEPT/limit)
is exhaustively covered against AgentExecutionService directly in
tests/test_review_orchestration_service.py (with a fake ProviderAdapter,
exactly like test_execution_service.py does for MA3). This file proves the
API/authorization/serialization layer on top of it: launching a reviewed
Task Run, authorization/project isolation, and the review-summary endpoint
correctly shaping whatever review-cycle state already exists in the DB.
"""

from decimal import Decimal

from app.db.enums import AgentRunRole, ReviewDecision
from app.models.reviews import AgentReview
from app.models.tasks import AgentRun


def _create_agent_and_publish(client, auth_headers, project_id, *, name="Runner", role="engineer") -> str:
    create = client.post(
        "/agents", headers=auth_headers, json={"project_id": project_id, "name": name, "role": role}
    )
    assert create.status_code == 201
    agent_id = create.json()["id"]
    version_resp = client.post(
        f"/agents/{agent_id}/versions", headers=auth_headers, json={"name": name, "role": role}
    )
    assert version_resp.status_code == 201
    publish = client.post(f"/agents/{agent_id}/versions/1/publish", headers=auth_headers)
    assert publish.status_code == 200
    return publish.json()["id"]


def _create_review_task(client, auth_headers, project_id, title="Write is_even"):
    resp = client.post(
        "/tasks",
        headers=auth_headers,
        json={"project_id": project_id, "title": title, "execution_mode": "build_review"},
    )
    assert resp.status_code == 201
    return resp.json()


def test_start_reviewed_task_run_requires_auth(client, bootstrap):
    resp = client.post(
        f"/tasks/{'x'}/runs/reviewed",
        json={"primary_agent_version_id": "a", "reviewer_agent_version_id": "b"},
    )
    assert resp.status_code == 401


def test_start_reviewed_task_run_creates_primary_agent_run_and_queues_it(client, auth_headers, bootstrap, db):
    primary_id = _create_agent_and_publish(
        client, auth_headers, bootstrap.project.id, name="Software Engineer", role="engineer"
    )
    reviewer_id = _create_agent_and_publish(
        client, auth_headers, bootstrap.project.id, name="Code Reviewer", role="reviewer"
    )
    task = _create_review_task(client, auth_headers, bootstrap.project.id)

    resp = client.post(
        f"/tasks/{task['id']}/runs/reviewed",
        headers=auth_headers,
        json={
            "primary_agent_version_id": primary_id,
            "reviewer_agent_version_id": reviewer_id,
            "max_repair_iterations": 3,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "queued"
    assert body["config_snapshot"]["primary_agent_version_id"] == primary_id
    assert body["config_snapshot"]["reviewer_agent_version_id"] == reviewer_id
    assert body["config_snapshot"]["max_repair_iterations"] == 3

    db.expire_all()
    runs = db.query(AgentRun).filter_by(task_run_id=body["id"]).all()
    assert len(runs) == 1
    assert runs[0].role == AgentRunRole.PRIMARY
    assert runs[0].agent_version_id == primary_id


def test_start_reviewed_task_run_defaults_repair_limit_when_omitted(client, auth_headers, bootstrap):
    from app.config import settings

    primary_id = _create_agent_and_publish(client, auth_headers, bootstrap.project.id, name="P1", role="p")
    reviewer_id = _create_agent_and_publish(client, auth_headers, bootstrap.project.id, name="R1", role="r")
    task = _create_review_task(client, auth_headers, bootstrap.project.id)

    resp = client.post(
        f"/tasks/{task['id']}/runs/reviewed",
        headers=auth_headers,
        json={"primary_agent_version_id": primary_id, "reviewer_agent_version_id": reviewer_id},
    )
    assert resp.status_code == 201
    assert resp.json()["config_snapshot"]["max_repair_iterations"] == settings.default_max_repair_iterations


def test_start_reviewed_task_run_rejects_non_build_review_task(client, auth_headers, bootstrap):
    primary_id = _create_agent_and_publish(client, auth_headers, bootstrap.project.id, name="P2", role="p")
    reviewer_id = _create_agent_and_publish(client, auth_headers, bootstrap.project.id, name="R2", role="r")
    single_agent_task = client.post(
        "/tasks",
        headers=auth_headers,
        json={"project_id": bootstrap.project.id, "title": "T", "execution_mode": "single_agent"},
    ).json()

    resp = client.post(
        f"/tasks/{single_agent_task['id']}/runs/reviewed",
        headers=auth_headers,
        json={"primary_agent_version_id": primary_id, "reviewer_agent_version_id": reviewer_id},
    )
    assert resp.status_code == 409


def test_start_reviewed_task_run_rejects_unpublished_agent_version(client, auth_headers, bootstrap):
    reviewer_id = _create_agent_and_publish(client, auth_headers, bootstrap.project.id, name="R3", role="r")
    draft_agent = client.post(
        "/agents",
        headers=auth_headers,
        json={"project_id": bootstrap.project.id, "name": "Draft", "role": "x"},
    ).json()
    draft_version = client.post(
        f"/agents/{draft_agent['id']}/versions", headers=auth_headers, json={"name": "Draft", "role": "x"}
    ).json()
    task = _create_review_task(client, auth_headers, bootstrap.project.id)

    resp = client.post(
        f"/tasks/{task['id']}/runs/reviewed",
        headers=auth_headers,
        json={"primary_agent_version_id": draft_version["id"], "reviewer_agent_version_id": reviewer_id},
    )
    assert resp.status_code == 409


def test_start_reviewed_task_run_cross_project_denied(client, db, auth_headers, bootstrap):
    from app.db.enums import ExecutionMode
    from tests.conftest import make_org, make_project, make_task

    other_project = make_project(db, org=make_org(db, name="Other Org MA4"))
    other_task = make_task(db, project=other_project, execution_mode=ExecutionMode.BUILD_REVIEW)
    db.commit()

    resp = client.post(
        f"/tasks/{other_task.id}/runs/reviewed",
        headers=auth_headers,
        json={"primary_agent_version_id": "x", "reviewer_agent_version_id": "y"},
    )
    assert resp.status_code == 403


# -- review-summary endpoint ---------------------------------------------------


def _seed_completed_review_cycle(db, bootstrap):
    """Builds a full accepted-after-one-repair review cycle directly via
    factories/models (the state machine itself is proven elsewhere) so the
    summary endpoint has real rows to shape."""
    from decimal import Decimal as D

    from app.db.enums import (
        AgentRunStatus,
        ArtifactType,
        ExecutionMode,
        ModelCallStatus,
        ProviderType,
        TaskRunStatus,
        VersionStatus,
    )
    from app.models.agents import Agent, AgentVersion
    from app.models.artifacts_eval import Artifact
    from app.models.execution import ModelCall
    from app.models.providers import Model, Provider, ProviderModel
    from app.models.tasks import Task, TaskRun

    project = bootstrap.project
    provider = Provider(type=ProviderType.OPENROUTER, name="Prov")
    db.add(provider)
    db.flush()
    model_a = Model(canonical_model_id="engineer/a")
    model_b = Model(canonical_model_id="reviewer/b")
    db.add_all([model_a, model_b])
    db.flush()
    pm_a = ProviderModel(
        model_id=model_a.id,
        provider_id=provider.id,
        provider_model_id="engineer/a",
        cost_input_per_mtok=D(0),
        cost_output_per_mtok=D(0),
    )
    pm_b = ProviderModel(
        model_id=model_b.id,
        provider_id=provider.id,
        provider_model_id="reviewer/b",
        cost_input_per_mtok=D(0),
        cost_output_per_mtok=D(0),
    )
    db.add_all([pm_a, pm_b])
    db.flush()

    primary_agent = Agent(project_id=project.id, name="Engineer", role="engineer")
    reviewer_agent = Agent(project_id=project.id, name="Reviewer", role="reviewer")
    db.add_all([primary_agent, reviewer_agent])
    db.flush()
    primary_version = AgentVersion(
        agent_id=primary_agent.id, version=1, name="Engineer", role="engineer", status=VersionStatus.ACTIVE
    )
    reviewer_version = AgentVersion(
        agent_id=reviewer_agent.id, version=1, name="Reviewer", role="reviewer", status=VersionStatus.ACTIVE
    )
    db.add_all([primary_version, reviewer_version])
    db.flush()

    task = Task(project_id=project.id, title="Write is_even", execution_mode=ExecutionMode.BUILD_REVIEW)
    db.add(task)
    db.flush()
    task_run = TaskRun(
        task_id=task.id,
        status=TaskRunStatus.COMPLETED,
        config_snapshot={
            "primary_agent_version_id": primary_version.id,
            "reviewer_agent_version_id": reviewer_version.id,
            "max_repair_iterations": 2,
        },
    )
    db.add(task_run)
    db.flush()

    primary_run = AgentRun(
        task_run_id=task_run.id,
        agent_version_id=primary_version.id,
        role=AgentRunRole.PRIMARY,
        status=AgentRunStatus.COMPLETED,
        model_id=model_a.id,
        provider_id=provider.id,
    )
    reviewer_run = AgentRun(
        task_run_id=task_run.id,
        agent_version_id=reviewer_version.id,
        role=AgentRunRole.REVIEWER,
        status=AgentRunStatus.COMPLETED,
        model_id=model_b.id,
        provider_id=provider.id,
    )
    db.add_all([primary_run, reviewer_run])
    db.flush()

    candidate_artifact = Artifact(
        agent_run_id=primary_run.id, type=ArtifactType.REPORT, storage_ref=__file__, content_hash="deadbeef"
    )
    db.add(candidate_artifact)
    db.flush()

    review = AgentReview(
        task_run_id=task_run.id,
        candidate_agent_run_id=primary_run.id,
        candidate_artifact_id=candidate_artifact.id,
        candidate_artifact_hash="deadbeef",
        reviewer_agent_run_id=reviewer_run.id,
        reviewer_agent_version_id=reviewer_version.id,
        iteration_number=0,
        decision=ReviewDecision.ACCEPT,
        summary="Looks good.",
        issues=[],
    )
    db.add(review)

    task_run.final_artifact_id = candidate_artifact.id

    db.add(
        ModelCall(
            agent_run_id=primary_run.id,
            model_id=model_a.id,
            provider_id=provider.id,
            status=ModelCallStatus.SUCCESS,
            tokens_in=100,
            tokens_out=40,
            cost_amount=D(0),
            cost_is_estimated=False,
        )
    )
    db.add(
        ModelCall(
            agent_run_id=reviewer_run.id,
            model_id=model_b.id,
            provider_id=provider.id,
            status=ModelCallStatus.SUCCESS,
            tokens_in=200,
            tokens_out=60,
            cost_amount=D(0),
            cost_is_estimated=False,
        )
    )
    db.commit()
    return task, task_run, candidate_artifact


def test_review_summary_shapes_full_cycle(client, auth_headers, bootstrap, db):
    task, task_run, candidate_artifact = _seed_completed_review_cycle(db, bootstrap)

    resp = client.get(f"/tasks/{task.id}/runs/{task_run.id}/review", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["outcome"] == "accepted"
    assert body["status"] == "completed"
    assert len(body["agent_runs"]) == 2
    roles = {r["role"] for r in body["agent_runs"]}
    assert roles == {"primary", "reviewer"}
    assert len(body["reviews"]) == 1
    assert body["reviews"][0]["decision"] == "accept"
    assert body["final_artifact"]["id"] == candidate_artifact.id
    assert Decimal(body["total_cost"]) == Decimal(0)
    usage_by_role = {u["role"]: u for u in body["usage"]}
    assert usage_by_role["primary"]["tokens_in"] == 100
    assert usage_by_role["reviewer"]["tokens_in"] == 200


def test_review_summary_404_for_non_review_task_run(client, auth_headers, bootstrap):
    agent_version_id = _create_agent_and_publish(
        client, auth_headers, bootstrap.project.id, name="SA", role="x"
    )
    task = client.post(
        "/tasks",
        headers=auth_headers,
        json={"project_id": bootstrap.project.id, "title": "T", "execution_mode": "single_agent"},
    ).json()
    run = client.post(
        f"/tasks/{task['id']}/runs", headers=auth_headers, json={"agent_version_id": agent_version_id}
    ).json()

    # A plain single_agent Task Run's config_snapshot has no review fields
    # at all -- the review-summary endpoint must not pretend one exists.
    resp = client.get(f"/tasks/{task['id']}/runs/{run['id']}/review", headers=auth_headers)
    assert resp.status_code == 404


def test_review_summary_requires_project_membership(client, db, auth_headers, bootstrap):
    from tests.conftest import make_org, make_project

    task, task_run, _ = _seed_completed_review_cycle(db, bootstrap)
    other_project = make_project(db, org=make_org(db, name="Other Org MA4 Summary"))
    task.project_id = other_project.id
    db.commit()

    resp = client.get(f"/tasks/{task.id}/runs/{task_run.id}/review", headers=auth_headers)
    assert resp.status_code == 403

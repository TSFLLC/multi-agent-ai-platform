"""Comparison Evaluation Fan-out HTTP surface — MA6 Slice 3C.

POST /comparisons/{comparison_id}/evaluations -- the domain endpoint
behind the future "Analyze Results" UI label (never the endpoint name
itself). The fan-out orchestration itself (eligibility, idempotency,
shared configuration, per-candidate failure isolation) is exhaustively
covered against EvaluationExecutionService.analyze_comparison directly in
tests/test_comparison_evaluation_fanout.py, exactly like
tests/test_comparison_api.py does for MA5's ComparisonService. This file
proves the API/authorization/serialization layer on top of it, mirroring
tests/test_evaluation_run_api.py's shape for the single-AgentRun endpoint.
"""

from dataclasses import dataclass, field
from typing import Any, List

from app.providers.base import InvokeRequest, InvokeResponse
from app.services.evaluation_definition_service import EvaluationDefinitionService
from app.services.execution_service import AgentExecutionService
from tests.conftest import make_evaluation_definition, make_org, make_project, make_task


@dataclass
class FakeAdapter:
    responses: List[Any] = field(default_factory=list)

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _factory_for(adapter: FakeAdapter):
    return lambda db, provider: adapter


def _run_candidate(db, agent_run_id: str, *, text: str = "OK") -> None:
    adapter = FakeAdapter(responses=[InvokeResponse(text=text, tokens_in=10, tokens_out=2)])
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(
        agent_run_id, worker_id="test-worker"
    )


def _publish_agent_with_free_model(client, auth_headers, project_id, *, name, role="engineer") -> str:
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


def _wire_free_model(db, agent_version_id: str) -> None:
    from decimal import Decimal as D

    from app.db.enums import ModelSelectionMode, ProviderType
    from app.models.agents import AgentVersion
    from app.models.providers import Model, Provider, ProviderModel

    provider = Provider(type=ProviderType.OPENROUTER, name=f"Provider-{agent_version_id}")
    db.add(provider)
    db.flush()
    model = Model(canonical_model_id=f"model/{agent_version_id}")
    db.add(model)
    db.flush()
    pm = ProviderModel(
        model_id=model.id,
        provider_id=provider.id,
        provider_model_id=model.canonical_model_id,
        cost_input_per_mtok=D(0),
        cost_output_per_mtok=D(0),
    )
    db.add(pm)
    db.flush()

    agent_version = db.get(AgentVersion, agent_version_id)
    agent_version.model_policy = {"mode": ModelSelectionMode.MANUAL.value, "manual_provider_model_id": pm.id}
    db.commit()


def _create_comparison_task(client, auth_headers, project_id, title="Compare candidates"):
    resp = client.post(
        "/tasks",
        headers=auth_headers,
        json={"project_id": project_id, "title": title, "execution_mode": "parallel_comparison"},
    )
    assert resp.status_code == 201
    return resp.json()


def _published_definition_version(db, project, criteria):
    definition = make_evaluation_definition(db, project=project)
    db.commit()
    version = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id, description=None, criteria=criteria
    )
    return EvaluationDefinitionService(db).publish_version(version.id)


def _criteria(*keys):
    return [{"key": k, "label": k.title()} for k in keys]


def _completed_comparison(client, auth_headers, bootstrap, db, *, suffix=""):
    av1 = _publish_agent_with_free_model(client, auth_headers, bootstrap.project.id, name=f"Engineer A{suffix}")
    av2 = _publish_agent_with_free_model(client, auth_headers, bootstrap.project.id, name=f"Engineer B{suffix}")
    _wire_free_model(db, av1)
    _wire_free_model(db, av2)
    task = _create_comparison_task(client, auth_headers, bootstrap.project.id, title=f"Compare{suffix}")
    comparison = client.post(
        "/comparisons",
        headers=auth_headers,
        json={
            "task_id": task["id"],
            "candidates": [{"agent_version_id": av1, "label": "A"}, {"agent_version_id": av2, "label": "B"}],
        },
    ).json()
    launched = client.post(f"/comparisons/{comparison['id']}/launch", headers=auth_headers).json()
    by_label = {c["label"]: c for c in launched["candidates"]}
    _run_candidate(db, by_label["A"]["agent_run_id"], text="RESULT_A")
    _run_candidate(db, by_label["B"]["agent_run_id"], text="RESULT_B")
    return launched


# -- auth / authorization ------------------------------------------------------


def test_analyze_comparison_requires_auth(client, bootstrap):
    resp = client.post("/comparisons/does-not-exist/evaluations", json={"evaluation_definition_version_id": "x"})
    assert resp.status_code == 401


def test_analyze_comparison_missing_comparison_returns_404(client, auth_headers, bootstrap):
    resp = client.post(
        "/comparisons/does-not-exist/evaluations",
        headers=auth_headers,
        json={"evaluation_definition_version_id": "does-not-matter"},
    )
    assert resp.status_code == 404


def test_analyze_comparison_cross_project_denied(client, db, auth_headers, bootstrap):
    other_project = make_project(db, org=make_org(db, name="Other Org 3C"))
    other_task = make_task(db, project=other_project)
    from app.models.tasks import TaskRun

    other_task_run = TaskRun(task_id=other_task.id)
    db.add(other_task_run)
    db.flush()
    from app.models.artifacts_eval import ComparisonRun

    other_comparison = ComparisonRun(task_run_id=other_task_run.id)
    db.add(other_comparison)
    db.commit()

    resp = client.post(
        f"/comparisons/{other_comparison.id}/evaluations",
        headers=auth_headers,
        json={"evaluation_definition_version_id": "does-not-matter"},
    )
    assert resp.status_code == 403


def test_analyze_comparison_agent_evaluator_requires_evaluator_agent_version_id(
    client, auth_headers, bootstrap, db
):
    launched = _completed_comparison(client, auth_headers, bootstrap, db)
    version = _published_definition_version(db, bootstrap.project, _criteria("non_empty_output"))

    resp = client.post(
        f"/comparisons/{launched['id']}/evaluations",
        headers=auth_headers,
        json={"evaluation_definition_version_id": version.id, "method": "agent_evaluator"},
    )
    assert resp.status_code == 422


# -- fan-out over real, completed candidates -------------------------------------


def test_analyze_comparison_deterministic_fanout_authorized(client, auth_headers, bootstrap, db):
    launched = _completed_comparison(client, auth_headers, bootstrap, db)
    version = _published_definition_version(db, bootstrap.project, _criteria("non_empty_output"))

    resp = client.post(
        f"/comparisons/{launched['id']}/evaluations",
        headers=auth_headers,
        json={"evaluation_definition_version_id": version.id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["comparison_id"] == launched["id"]
    assert len(body["results"]) == 2
    assert {r["status"] for r in body["results"]} == {"created"}
    run_ids = {r["evaluation_run_id"] for r in body["results"]}
    assert len(run_ids) == 2  # independent per candidate
    subject_agent_run_ids = {r["subject_agent_run_id"] for r in body["results"]}
    expected_agent_run_ids = {c["agent_run_id"] for c in launched["candidates"]}
    assert subject_agent_run_ids == expected_agent_run_ids


def test_analyze_comparison_read_api_exposes_criterion_results_and_evaluator_provenance(
    client, auth_headers, bootstrap, db
):
    launched = _completed_comparison(client, auth_headers, bootstrap, db)
    version = _published_definition_version(db, bootstrap.project, _criteria("non_empty_output"))

    resp = client.post(
        f"/comparisons/{launched['id']}/evaluations",
        headers=auth_headers,
        json={"evaluation_definition_version_id": version.id},
    )
    assert resp.status_code == 200
    run_id = resp.json()["results"][0]["evaluation_run_id"]

    # Execute the deterministic EvaluationRun directly -- launch_comparison
    # already left each candidate's own JobType.AGENT_RUN job queued (its
    # candidate Agent Run was executed directly above, not drained from the
    # queue), so a queue-draining fast_worker.run_once() here would race
    # that leftover job instead of this EvaluationRun's own JobType.
    # EVALUATION job; app.services.evaluation_execution_service.
    # EvaluationExecutionService.execute is the same call the Worker itself
    # makes for JobType.EVALUATION.
    from app.services.evaluation_execution_service import EvaluationExecutionService

    EvaluationExecutionService(db).execute(run_id, worker_id="test-worker")

    get_resp = client.get(f"/evaluation-runs/{run_id}", headers=auth_headers)
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["method"] == "deterministic"
    assert body["status"] == "completed"
    assert body["subject_agent_run_id"] in {c["agent_run_id"] for c in launched["candidates"]}
    assert body["subject_artifact_content_hash"]
    assert body["evaluation_definition_version_id"] == version.id
    assert len(body["criterion_results"]) == 1
    assert body["criterion_results"][0]["finding"] == "met"


def test_analyze_comparison_retry_via_http_is_idempotent(client, auth_headers, bootstrap, db):
    launched = _completed_comparison(client, auth_headers, bootstrap, db)
    version = _published_definition_version(db, bootstrap.project, _criteria("non_empty_output"))
    body = {"evaluation_definition_version_id": version.id}

    first = client.post(f"/comparisons/{launched['id']}/evaluations", headers=auth_headers, json=body)
    assert first.status_code == 200
    first_results = {r["comparison_candidate_id"]: r["evaluation_run_id"] for r in first.json()["results"]}

    second = client.post(f"/comparisons/{launched['id']}/evaluations", headers=auth_headers, json=body)
    assert second.status_code == 200
    second_results = {r["comparison_candidate_id"]: r["evaluation_run_id"] for r in second.json()["results"]}
    assert second_results == first_results
    assert {r["status"] for r in second.json()["results"]} == {"reused"}


# -- GET .../evaluations: read-only history, Slice 3D ----------------------------


def test_list_comparison_evaluations_requires_auth(client, bootstrap):
    resp = client.get("/comparisons/does-not-exist/evaluations")
    assert resp.status_code == 401


def test_list_comparison_evaluations_missing_comparison_returns_404(client, auth_headers, bootstrap):
    resp = client.get("/comparisons/does-not-exist/evaluations", headers=auth_headers)
    assert resp.status_code == 404


def test_list_comparison_evaluations_cross_project_denied(client, db, auth_headers, bootstrap):
    other_project = make_project(db, org=make_org(db, name="Other Org 3D"))
    other_task = make_task(db, project=other_project)
    from app.models.artifacts_eval import ComparisonRun
    from app.models.tasks import TaskRun

    other_task_run = TaskRun(task_id=other_task.id)
    db.add(other_task_run)
    db.flush()
    other_comparison = ComparisonRun(task_run_id=other_task_run.id)
    db.add(other_comparison)
    db.commit()

    resp = client.get(f"/comparisons/{other_comparison.id}/evaluations", headers=auth_headers)
    assert resp.status_code == 403


def test_list_comparison_evaluations_maps_runs_to_correct_candidates(client, auth_headers, bootstrap, db):
    launched = _completed_comparison(client, auth_headers, bootstrap, db)
    version = _published_definition_version(db, bootstrap.project, _criteria("non_empty_output"))

    fanout = client.post(
        f"/comparisons/{launched['id']}/evaluations",
        headers=auth_headers,
        json={"evaluation_definition_version_id": version.id},
    )
    assert fanout.status_code == 200
    expected_run_id_by_candidate = {
        r["comparison_candidate_id"]: r["evaluation_run_id"] for r in fanout.json()["results"]
    }

    resp = client.get(f"/comparisons/{launched['id']}/evaluations", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["comparison_id"] == launched["id"]
    assert len(body["candidates"]) == 2

    for entry in body["candidates"]:
        candidate_id = entry["comparison_candidate_id"]
        run_ids = [r["id"] for r in entry["evaluation_runs"]]
        assert run_ids == [expected_run_id_by_candidate[candidate_id]]
        # never a comparative/ranking field anywhere in this response
        for run in entry["evaluation_runs"]:
            assert "score" not in run and "rank" not in run and "winner" not in run

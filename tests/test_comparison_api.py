"""Comparison HTTP surface — MA5.

The orchestration state machine itself (candidate isolation, budget
sharing, phase derivation, cancellation, canonical selection) is
exhaustively covered against ComparisonService directly in
tests/test_comparison_service.py, exactly like test_reviewed_task_run_api.py
does for MA4's review cycle vs. test_review_orchestration_service.py. This
file proves the API/authorization/serialization layer on top of it.
"""

from dataclasses import dataclass, field
from typing import Any, List

from app.providers.base import InvokeRequest, InvokeResponse
from app.services.execution_service import AgentExecutionService


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
    """Publishes an Agent Version wired to a real (test) FREE manual model
    via direct DB manipulation -- the /agents API (MA2) does not expose
    model_policy directly, so it is set the same way
    test_reviewed_task_run_api.py's fixtures set up execution-ready Agent
    Versions: publish through the API, then attach a resolvable model."""
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


# -- auth / authorization ------------------------------------------------------


def test_create_comparison_requires_auth(client, bootstrap):
    resp = client.post("/comparisons", json={"task_id": "x", "candidates": []})
    assert resp.status_code == 401


def test_get_comparison_requires_auth(client, bootstrap):
    resp = client.get("/comparisons/does-not-exist")
    assert resp.status_code == 401


def test_create_comparison_cross_project_denied(client, db, auth_headers, bootstrap):
    from app.db.enums import ExecutionMode
    from tests.conftest import make_org, make_project, make_task

    other_project = make_project(db, org=make_org(db, name="Other Org MA5"))
    other_task = make_task(db, project=other_project, execution_mode=ExecutionMode.PARALLEL_COMPARISON)
    db.commit()

    resp = client.post(
        "/comparisons",
        headers=auth_headers,
        json={"task_id": other_task.id, "candidates": [{"agent_version_id": "a", "label": "A"}]},
    )
    assert resp.status_code == 403


# -- create / launch lifecycle ---------------------------------------------------


def test_create_comparison_configures_without_launching(client, auth_headers, bootstrap, db):
    av1 = _publish_agent_with_free_model(client, auth_headers, bootstrap.project.id, name="Engineer A")
    av2 = _publish_agent_with_free_model(client, auth_headers, bootstrap.project.id, name="Engineer B")
    _wire_free_model(db, av1)
    _wire_free_model(db, av2)
    task = _create_comparison_task(client, auth_headers, bootstrap.project.id)

    resp = client.post(
        "/comparisons",
        headers=auth_headers,
        json={
            "task_id": task["id"],
            "candidates": [
                {"agent_version_id": av1, "label": "Candidate A"},
                {"agent_version_id": av2, "label": "Candidate B"},
            ],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["phase"] == "pending"
    assert body["task_id"] == task["id"]
    assert len(body["candidates"]) == 2
    assert all(c["status"] == "not_launched" for c in body["candidates"])
    assert all(c["task_run_id"] is None for c in body["candidates"])


def test_create_comparison_rejects_fewer_than_two_candidates(client, auth_headers, bootstrap, db):
    av1 = _publish_agent_with_free_model(client, auth_headers, bootstrap.project.id, name="Solo")
    task = _create_comparison_task(client, auth_headers, bootstrap.project.id)

    resp = client.post(
        "/comparisons",
        headers=auth_headers,
        json={"task_id": task["id"], "candidates": [{"agent_version_id": av1, "label": "A"}]},
    )
    assert resp.status_code == 409


def test_create_comparison_rejects_non_parallel_comparison_task(client, auth_headers, bootstrap):
    av1 = _publish_agent_with_free_model(client, auth_headers, bootstrap.project.id, name="A")
    av2 = _publish_agent_with_free_model(client, auth_headers, bootstrap.project.id, name="B")
    single_task = client.post(
        "/tasks",
        headers=auth_headers,
        json={"project_id": bootstrap.project.id, "title": "T", "execution_mode": "single_agent"},
    ).json()

    resp = client.post(
        "/comparisons",
        headers=auth_headers,
        json={
            "task_id": single_task["id"],
            "candidates": [{"agent_version_id": av1, "label": "A"}, {"agent_version_id": av2, "label": "B"}],
        },
    )
    assert resp.status_code == 409


def test_launch_creates_runnable_candidates_and_is_idempotent(client, auth_headers, bootstrap, db):
    av1 = _publish_agent_with_free_model(client, auth_headers, bootstrap.project.id, name="Engineer A2")
    av2 = _publish_agent_with_free_model(client, auth_headers, bootstrap.project.id, name="Engineer B2")
    _wire_free_model(db, av1)
    _wire_free_model(db, av2)
    task = _create_comparison_task(client, auth_headers, bootstrap.project.id)
    create_resp = client.post(
        "/comparisons",
        headers=auth_headers,
        json={
            "task_id": task["id"],
            "candidates": [{"agent_version_id": av1, "label": "A"}, {"agent_version_id": av2, "label": "B"}],
        },
    ).json()
    comparison_id = create_resp["id"]

    idem_key = "launch-key-1"
    launch1 = client.post(
        f"/comparisons/{comparison_id}/launch",
        headers={**auth_headers, "Idempotency-Key": idem_key},
    )
    assert launch1.status_code == 200
    body = launch1.json()
    assert body["status"] == "running"
    assert all(c["task_run_id"] is not None and c["agent_run_id"] is not None for c in body["candidates"])
    assert all(c["status"] == "queued" for c in body["candidates"])

    # Retried with the SAME Idempotency-Key: returns the same result, does
    # not launch a second time.
    launch2 = client.post(
        f"/comparisons/{comparison_id}/launch",
        headers={**auth_headers, "Idempotency-Key": idem_key},
    )
    assert launch2.status_code == 200
    assert launch2.json()["candidates"] == body["candidates"]

    # A genuinely new launch attempt (fresh key) against an already-RUNNING
    # comparison is rejected -- launching is a one-shot transition.
    launch3 = client.post(f"/comparisons/{comparison_id}/launch", headers=auth_headers)
    assert launch3.status_code == 409


# -- full lifecycle: launch -> execute -> select-winner --------------------------


def _launched_comparison(client, auth_headers, bootstrap, db):
    av1 = _publish_agent_with_free_model(client, auth_headers, bootstrap.project.id, name="Lifecycle A")
    av2 = _publish_agent_with_free_model(client, auth_headers, bootstrap.project.id, name="Lifecycle B")
    _wire_free_model(db, av1)
    _wire_free_model(db, av2)
    task = _create_comparison_task(client, auth_headers, bootstrap.project.id)
    comparison = client.post(
        "/comparisons",
        headers=auth_headers,
        json={
            "task_id": task["id"],
            "candidates": [{"agent_version_id": av1, "label": "A"}, {"agent_version_id": av2, "label": "B"}],
        },
    ).json()
    launched = client.post(f"/comparisons/{comparison['id']}/launch", headers=auth_headers).json()
    return launched


def test_full_lifecycle_ready_for_selection_and_select_winner(client, auth_headers, bootstrap, db):
    launched = _launched_comparison(client, auth_headers, bootstrap, db)
    candidates_by_label = {c["label"]: c for c in launched["candidates"]}

    _run_candidate(db, candidates_by_label["A"]["agent_run_id"], text="RESULT_A")
    _run_candidate(db, candidates_by_label["B"]["agent_run_id"], text="RESULT_B")

    detail = client.get(f"/comparisons/{launched['id']}", headers=auth_headers).json()
    assert detail["phase"] == "ready_for_selection"
    by_label = {c["label"]: c for c in detail["candidates"]}
    assert by_label["A"]["status"] == "completed"
    assert by_label["A"]["tokens_in"] == 10
    assert by_label["A"]["artifact_hash"] is not None
    assert detail["total_tokens_in"] == by_label["A"]["tokens_in"] + by_label["B"]["tokens_in"]

    winner = by_label["A"]
    wrong_hash = client.post(
        f"/comparisons/{launched['id']}/select-winner",
        headers=auth_headers,
        json={"comparison_candidate_id": winner["id"], "artifact_hash": "not-the-real-hash"},
    )
    assert wrong_hash.status_code == 409
    assert wrong_hash.json()["error"]["code"] == "artifact_hash_mismatch"

    selected = client.post(
        f"/comparisons/{launched['id']}/select-winner",
        headers=auth_headers,
        json={"comparison_candidate_id": winner["id"], "artifact_hash": winner["artifact_hash"]},
    )
    assert selected.status_code == 200
    body = selected.json()
    assert body["status"] == "completed"
    assert body["winner_agent_run_id"] == winner["agent_run_id"]
    assert body["winner_artifact_hash"] == winner["artifact_hash"]
    assert next(c for c in body["candidates"] if c["id"] == winner["id"])["is_winner"] is True

    # A second selection attempt against an already-COMPLETED comparison is
    # refused -- the platform never lets the canonical result silently change.
    again = client.post(
        f"/comparisons/{launched['id']}/select-winner",
        headers=auth_headers,
        json={"comparison_candidate_id": winner["id"], "artifact_hash": winner["artifact_hash"]},
    )
    assert again.status_code == 409


def test_select_winner_rejected_while_still_running(client, auth_headers, bootstrap, db):
    launched = _launched_comparison(client, auth_headers, bootstrap, db)
    candidates_by_label = {c["label"]: c for c in launched["candidates"]}
    # Only ONE candidate finishes -- the other is still "queued".
    _run_candidate(db, candidates_by_label["A"]["agent_run_id"], text="RESULT_A")

    resp = client.post(
        f"/comparisons/{launched['id']}/select-winner",
        headers=auth_headers,
        json={"comparison_candidate_id": candidates_by_label["A"]["id"], "artifact_hash": "whatever"},
    )
    assert resp.status_code == 409


def test_cancel_comparison_via_api(client, auth_headers, bootstrap, db):
    launched = _launched_comparison(client, auth_headers, bootstrap, db)

    resp = client.post(f"/comparisons/{launched['id']}/cancel", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancellation_requested_at"] is not None
    assert body["status"] == "running"  # cooperative, not instant

    again = client.post(f"/comparisons/{launched['id']}/cancel", headers=auth_headers)
    assert again.status_code == 409

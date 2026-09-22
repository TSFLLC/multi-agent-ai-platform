"""MA7.6B -- the backend behind the Live Control Room.

Runs go through the REAL engine, queue and Worker with only the provider faked
(the MA7.5B/5C harness), then the read models and the existing decision/cancel
routes are exercised over HTTP exactly as the Control Room uses them.

A. ``GET /workflow-runs/{id}/detail``: one consistent snapshot of the bound version.
B. Agent != Model, evaluation and approval references, output artifacts.
C. Usage and cost: per node and per run, exact Decimal arithmetic, estimated flag.
D. Failure information (agent run, evaluation, dispatch-time).
E. ``GET /workflows/{id}/runs``: factual run history across versions.
F. Human Approval through the existing routes: approve / reject / replay / conflict.
G. Cancellation.
H. Authorization and isolation.
I. No score / rank / winner / verdict / recommendation anywhere.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.db.enums import (
    ProjectRole,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
    WorkflowRunStatus,
)
from app.models.evaluation_runs import EvaluationRun
from app.models.execution import ModelCall
from app.models.governance import Approval
from app.models.identity import Project, ProjectMembership
from app.models.tasks import AgentRun, Task
from app.models.workflow import WorkflowNodeRun, WorkflowRun
from app.providers.base import ProviderInvalidResponseError
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.workflow_execution_service import WorkflowExecutionService
from app.services.workflow_run_read_service import aggregate_usage
from tests.conftest import make_agent, make_runnable_agent_version, make_task
from tests.ma7_3b_support import resolve_via_api
from tests.ma7_4b_support import new_worker
from tests.test_ma7_5a_evaluation_node import counts, rows
from tests.test_ma7_5b_parallel_evaluations import (
    EVALS,
    GATE_PARENTS,
    REVIEWERS,
    build_parallel,
    drain,
    go,
    state_of,
    sweep,
)
from tests.test_ma7_5c_final_uat import AGENT_NODES, USAGE, expected_cost, meter

NODE_KEYS = sorted([*AGENT_NODES, "gate", "result"])


@pytest.fixture()
def artifacts_dir(monkeypatch, tmp_path):
    from app.config import settings

    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


# -- helpers ---------------------------------------------------------------------------------


def flagship(db, session_factory, monkeypatch, bootstrap, label, *, metered=True, **kwargs):
    """The MA7.5 flagship graph driven by the real Worker to its waiting gate."""
    harness = build_parallel(db, bootstrap.project, monkeypatch, label, **kwargs)
    if metered:
        meter(db, harness)
    go(db, harness)
    drain(new_worker(session_factory))
    return harness


def detail(client, headers, run_id):
    response = client.get(f"/workflow-runs/{run_id}/detail", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def by_key(body):
    return {node["node_key"]: node for node in body["nodes"]}


def dec(value):
    return Decimal(str(value))


def foreign_project(db, bootstrap, name, member=False):
    project = Project(org_id=bootstrap.organization.id, name=name)
    db.add(project)
    db.flush()
    if member:
        db.add(ProjectMembership(project_id=project.id, user_id=bootstrap.user.id, role=ProjectRole.OWNER))
    db.commit()
    return project


def foreign_run(db, bootstrap, name="hostile"):
    """A workflow RUN in a project the user has no membership in."""
    other = foreign_project(db, bootstrap, name)
    service = WorkflowDefinitionService(db)
    workflow = service.create_workflow(other.id, name)
    av = make_runnable_agent_version(db, make_agent(db, other, name))
    a = service.add_node(workflow.id, 1, "a", WorkflowNodeType.AGENT, config={"agent_version_id": av.id})
    done = service.add_node(workflow.id, 1, "done", WorkflowNodeType.TERMINAL)
    service.add_edge(workflow.id, 1, a.id, done.id)
    service.publish_version(workflow.id, 1)
    task = make_task(db, other)
    db.commit()
    version = service.get_workflow_version(workflow.id, 1)
    run = WorkflowExecutionService(db).start_workflow_run_from_task(version.id, task.id)
    return SimpleNamespace(workflow=workflow, run=run, project=other, task=task)


# =============================================================================
# A. The run snapshot
# =============================================================================


def test_the_detail_is_one_snapshot_of_the_exact_bound_version(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "a1")
    body = detail(client, auth_headers, harness.run.id)

    assert body["id"] == harness.run.id and body["status"] == "node_waiting_for_approval"
    assert body["workflow_version_id"] == harness.built.published.id  # the exact bound version
    assert body["workflow_version"] == 1 and body["workflow_version_status"] == "active"
    assert body["workflow_id"] == harness.built.workflow.id and body["workflow_name"] == harness.built.workflow.name
    assert body["task_run_id"] == harness.built.task_run.id
    assert body["started_at"] is not None and body["ended_at"] is None and body["cancellation_requested_at"] is None
    assert sorted(by_key(body)) == NODE_KEYS
    assert len(body["edges"]) == 14  # the graph of THIS version, not the current one


def test_node_states_are_the_backends_own_statuses(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "a2")
    nodes = by_key(detail(client, auth_headers, harness.run.id))

    assert {nodes[key]["status"] for key in AGENT_NODES} == {"completed"}
    assert nodes["gate"]["status"] == "waiting_for_approval"  # a person is needed
    assert nodes["result"]["status"] == "pending"  # not reached yet
    for node in nodes.values():
        assert node["status"] in {s.value for s in WorkflowNodeRunStatus}  # nothing invented
    assert all(nodes[key]["duration_seconds"] is not None and nodes[key]["duration_seconds"] >= 0 for key in AGENT_NODES)
    assert nodes["gate"]["ended_at"] is None and nodes["result"]["started_at"] is None


def test_a_run_that_just_started_shows_pending_nodes_and_no_usage_at_all(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "a3")
    go(db, harness)  # started; nothing has executed yet
    body = detail(client, auth_headers, harness.run.id)

    nodes = by_key(body)
    assert nodes["planner"]["status"] == "running" and nodes["planner"]["agent"]["status"] == "created"  # queued
    progress = nodes["planner"]["agent"]["progress"]
    assert progress["stage"] == "Task dispatched"
    assert progress["attempt_number"] is None
    assert progress["last_activity_at"] is not None
    assert {nodes[key]["status"] for key in NODE_KEYS if key != "planner"} == {"pending"}
    assert body["usage"] is None and all(node["usage"] is None for node in body["nodes"])  # never zeros


def test_fan_out_and_fan_in_are_visible_in_the_edges(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "a4")
    body = detail(client, auth_headers, harness.run.id)
    key_of = {node["node_id"]: node["node_key"] for node in body["nodes"]}
    pairs = {(key_of[e["from_node_id"]], key_of[e["to_node_id"]]) for e in body["edges"]}

    assert {b for a, b in pairs if a == "engineer"} == set(REVIEWERS)  # fan-out
    assert sorted(a for a, b in pairs if b == "gate") == GATE_PARENTS  # fan-in: all six feed the gate
    assert {b for a, b in pairs if a == "gate"} == {"result"}


def test_the_snapshot_reflects_the_run_binding_not_the_latest_version(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "a5")
    service = WorkflowDefinitionService(db)
    clone = service.clone_version(harness.built.workflow.id, 1)  # a newer DRAFT exists ...
    service.add_node(harness.built.workflow.id, clone.version, "extra", WorkflowNodeType.TERMINAL)  # ... and is edited
    body = detail(client, auth_headers, harness.run.id)
    assert clone.version == 2
    assert body["workflow_version"] == 1 and len(body["nodes"]) == 10 and len(body["edges"]) == 14
    assert "extra" not in by_key(body)


# =============================================================================
# B. Agent != Model, evaluation and approval references, outputs
# =============================================================================


def test_agent_and_model_are_reported_separately(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "b1")
    nodes = by_key(detail(client, auth_headers, harness.run.id))

    for key in AGENT_NODES:
        agent = nodes[key]["agent"]
        assert agent["agent_name"] == (f"b1-{key}-evaluator" if key in EVALS.values() else f"b1-{key}")
        assert agent["agent_version"] == 1 and agent["role"] and agent["agent_id"] != agent["agent_version_id"]
        model = agent["model"]
        assert model["canonical_model_id"] and model["provider_name"] and model["provider_model_snapshot_id"]
        assert model["model_id"] not in (agent["agent_id"], agent["agent_version_id"])  # a model is not an agent
        assert agent["status"] == "completed" and agent["started_at"] and agent["ended_at"]
    assert nodes["planner"]["agent"]["model"]["canonical_model_id"] == "fake/b1-planner"
    assert nodes["gate"]["agent"] is None and nodes["result"]["agent"] is None  # no AgentRun for a gate / terminal


def test_evaluation_nodes_reference_their_evaluation_run_and_evaluator(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(
        db,
        session_factory,
        monkeypatch,
        bootstrap,
        "b2",
        findings={"eval_security": {"security": "NOT_MET"}, "eval_code": {"correctness": "PARTIAL"}},
    )
    body = detail(client, auth_headers, harness.run.id)
    nodes = by_key(body)
    runs = {e.evaluator_agent_run_id: e for e in rows(session_factory, EvaluationRun)}

    for node_key in EVALS.values():
        node = nodes[node_key]
        assert node["agent"]["run_role"] == "evaluator"
        assert node["evaluation_status"] == "completed"
        assert node["evaluation_run_id"] == runs[node["agent"]["agent_run_id"]].id
    for key in REVIEWERS:
        assert nodes[key]["evaluation_run_id"] is None and nodes[key]["agent"]["run_role"] is None

    # findings are read through the evaluation evidence route: MET/PARTIAL/NOT_MET, per criterion, no verdict
    security = client.get(f"/evaluation-runs/{nodes['eval_security']['evaluation_run_id']}", headers=auth_headers).json()
    assert {r["criterion_key"]: r["finding"] for r in security["criterion_results"]} == {"correctness": "met", "security": "not_met"}
    assert security["subject"]["agent_run_id"] == nodes["security"]["agent"]["agent_run_id"]
    assert security["subject"]["artifact_id"] == nodes["security"]["output_artifact_id"]
    assert security["evaluator"]["agent_run_id"] == nodes["eval_security"]["agent"]["agent_run_id"]
    # a NOT_MET / PARTIAL finding decides nothing
    assert body["status"] == "node_waiting_for_approval" and nodes["gate"]["approval_status"] == "pending"


def test_outputs_are_the_completed_nodes_artifacts_and_are_readable(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "b3")
    nodes = by_key(detail(client, auth_headers, harness.run.id))
    state = state_of(session_factory, harness)

    for key in AGENT_NODES:
        assert nodes[key]["output_artifact_id"] == state["node_runs"][key].output_snapshot_ref
    assert nodes["gate"]["output_artifact_id"] is None  # a multi-parent gate has no output of its own
    content = client.get(f"/artifacts/{nodes['code']['output_artifact_id']}/content", headers=auth_headers)
    assert content.status_code == 200 and content.text == "OUTPUT-OF-code"


def test_the_approval_is_referenced_and_its_evidence_is_the_six_bound_outputs(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "b4")
    nodes = by_key(detail(client, auth_headers, harness.run.id))
    approval_id = nodes["gate"]["approval_id"]
    assert approval_id and nodes["gate"]["approval_status"] == "pending"
    assert all(nodes[key]["approval_id"] is None for key in AGENT_NODES)

    evidence = client.get(f"/approvals/{approval_id}/evidence", headers=auth_headers).json()
    assert [e["node_key"] for e in evidence["evidence"]] == GATE_PARENTS  # deterministic, six entries
    assert evidence["fingerprint_matches"] is True and all(e["content_intact"] for e in evidence["evidence"])
    for entry in evidence["evidence"]:
        assert entry["artifact_id"] == nodes[entry["node_key"]]["output_artifact_id"]


# =============================================================================
# C. Usage and cost
# =============================================================================


def test_usage_is_summed_per_node_and_per_run_with_exact_decimal_arithmetic(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "c1")
    body = detail(client, auth_headers, harness.run.id)
    nodes = by_key(body)

    total_cost = Decimal(0)
    for key in AGENT_NODES:
        usage = nodes[key]["usage"]
        tokens_in, tokens_out = USAGE[key]
        amount, estimated = expected_cost(key)
        assert (usage["tokens_in"], usage["tokens_out"], usage["total_tokens"]) == (tokens_in, tokens_out, tokens_in + tokens_out)
        assert abs(dec(usage["cost_amount"]) - amount) <= Decimal("0.0000001")
        assert usage["cost_is_estimated"] is estimated and usage["model_call_count"] == 1 and usage["cost_currency"] == "USD"
        total_cost += dec(usage["cost_amount"])
    total = body["usage"]
    assert total["tokens_in"] == sum(u[0] for u in USAGE.values()) and total["tokens_out"] == sum(u[1] for u in USAGE.values())
    assert total["total_tokens"] == total["tokens_in"] + total["tokens_out"]
    assert dec(total["cost_amount"]) == total_cost  # the total is exactly the sum of the nodes' figures
    assert total["model_call_count"] == 8
    # and it agrees with the underlying model_calls rows
    calls = rows(session_factory, ModelCall)
    assert len(calls) == 8 and sum((dec(c.cost_amount) for c in calls), Decimal(0)) == dec(total["cost_amount"])
    assert nodes["gate"]["usage"] is None and nodes["result"]["usage"] is None


def test_a_total_that_includes_an_estimate_says_so(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "c2")
    body = detail(client, auth_headers, harness.run.id)
    assert body["usage"]["cost_is_estimated"] is True  # some calls are priced estimates
    nodes = by_key(body)
    assert nodes["security"]["usage"]["cost_is_estimated"] is False  # ... while a provider-reported cost is exact
    assert nodes["planner"]["usage"]["cost_is_estimated"] is False  # ... and a verified-FREE model's zero is exact
    assert nodes["engineer"]["usage"]["cost_is_estimated"] is True


def test_an_exact_total_is_not_flagged_as_estimated(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    """A run of verified-FREE models only: every call is a known zero, so nothing is estimated."""
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "c2free", metered=False)
    total = detail(client, auth_headers, harness.run.id)["usage"]
    assert dec(total["cost_amount"]) == 0 and total["cost_is_estimated"] is False and total["total_tokens"] == 8 * 15


def test_a_second_runs_usage_never_leaks_into_the_first(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "c3")
    first = detail(client, auth_headers, harness.run.id)["usage"]
    task = make_task(db, bootstrap.project)
    db.commit()
    second_run = WorkflowExecutionService(db).start_workflow_run_from_task(harness.built.published.id, task.id)
    drain(new_worker(session_factory))

    assert detail(client, auth_headers, harness.run.id)["usage"] == first  # unchanged by the other run
    second = detail(client, auth_headers, second_run.id)["usage"]
    assert second["total_tokens"] == first["total_tokens"] and second["model_call_count"] == 8
    assert len(rows(session_factory, ModelCall)) == 16


def test_aggregate_usage_is_decimal_safe_and_honest():
    def call(tin, tout, cost, estimated, currency="USD"):
        return SimpleNamespace(tokens_in=tin, tokens_out=tout, cost_amount=cost, cost_is_estimated=estimated, cost_currency=currency)

    assert aggregate_usage([]) is None  # no calls: not zeros
    exact = aggregate_usage([call(1, 2, Decimal("0.1"), False), call(3, 4, Decimal("0.2"), False)])
    assert exact.cost_amount == Decimal("0.3") and exact.cost_is_estimated is False  # 0.1 + 0.2, no float drift
    assert (exact.tokens_in, exact.tokens_out, exact.total_tokens, exact.model_call_count) == (4, 6, 10, 2)
    mixed_estimate = aggregate_usage([call(1, 1, Decimal(1), False), call(1, 1, Decimal(2), True)])
    assert mixed_estimate.cost_is_estimated is True and mixed_estimate.cost_amount == Decimal(3)
    from_float = aggregate_usage([call(1, 1, 0.1, False), call(1, 1, 0.2, False)])  # a driver may hand back floats
    assert from_float.cost_amount == Decimal("0.3")
    failed = aggregate_usage([call(None, None, None, True), call(5, 5, Decimal("0.5"), False)])  # a failed call recorded nothing
    assert (failed.tokens_in, failed.model_call_count, failed.cost_amount) == (5, 2, Decimal("0.5"))
    other = aggregate_usage([call(1, 1, Decimal(1), False, "USD"), call(1, 1, Decimal(1), False, "EUR")])
    assert other.cost_amount is None and other.cost_currency == "mixed"  # currencies are never added


# =============================================================================
# D. Failure information
# =============================================================================


def test_a_failed_reviewer_reports_the_recorded_error_and_preserves_the_rest(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "d1")
    harness.provider.hooks["security"] = lambda _r: (_ for _ in ()).throw(ProviderInvalidResponseError("boom"))
    go(db, harness)
    drain(new_worker(session_factory))
    body = detail(client, auth_headers, harness.run.id)
    nodes = by_key(body)

    assert body["status"] == "failed" and body["ended_at"] is not None
    assert nodes["security"]["status"] == "failed" and nodes["security"]["agent"]["status"] == "failed"
    assert nodes["security"]["failure"]["source"] == "agent_run"
    assert nodes["security"]["failure"]["category"] == "provider_invalid_response"
    assert nodes["code"]["status"] == "completed" and nodes["code"]["output_artifact_id"]  # completed work is preserved
    assert nodes["gate"]["status"] == "pending" and nodes["gate"]["approval_id"] is None
    assert nodes["code"]["failure"] is None


def test_a_failed_evaluation_reports_its_reason_without_any_artifact_content(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "d2", texts={"eval_code": "SECRET-RAW-OUTPUT not json"})
    go(db, harness)
    drain(new_worker(session_factory))
    node = by_key(detail(client, auth_headers, harness.run.id))["eval_code"]

    assert node["status"] == "failed" and node["evaluation_status"] == "failed"
    assert node["failure"]["source"] == "evaluation" and node["failure"]["message"]
    assert "SECRET-RAW-OUTPUT" not in node["failure"]["message"]


def test_a_node_that_cannot_be_dispatched_reports_the_workflow_level_reason(
    client, db, session_factory, auth_headers, bootstrap
):
    from app.db.enums import WorkflowNodeType
    from tests.conftest import make_agent

    service = WorkflowDefinitionService(db)
    workflow = service.create_workflow(bootstrap.project.id, "d3")
    av = make_runnable_agent_version(db, make_agent(db, bootstrap.project, "d3"))
    judge = service.add_node(workflow.id, 1, "judge_it", WorkflowNodeType.JUDGE, config={"judge_agent_version_id": av.id})
    done = service.add_node(workflow.id, 1, "done", WorkflowNodeType.TERMINAL)
    service.add_edge(workflow.id, 1, judge.id, done.id)
    service.publish_version(workflow.id, 1)
    task = make_task(db, bootstrap.project)
    db.commit()
    run = WorkflowExecutionService(db).start_workflow_run_from_task(service.get_workflow_version(workflow.id, 1).id, task.id)

    nodes = by_key(detail(client, auth_headers, run.id))
    assert nodes["judge_it"]["status"] == "failed" and nodes["judge_it"]["node_type"] == "judge"
    assert nodes["judge_it"]["failure"]["source"] == "workflow"
    assert "Unsupported node type" in nodes["judge_it"]["failure"]["message"]
    assert nodes["judge_it"]["agent"] is None


# =============================================================================
# E. Run history
# =============================================================================


def test_run_history_lists_every_run_across_versions_newest_first_with_facts_only(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "e1")
    workflow_id = harness.built.workflow.id
    service = WorkflowDefinitionService(db)
    v2 = service.clone_version(workflow_id, 1)
    service.publish_version(workflow_id, v2.version)
    task = make_task(db, bootstrap.project)
    task.title = "Second assignment"
    db.commit()
    newer = WorkflowExecutionService(db).start_workflow_run_from_task(v2.id, task.id)  # bound to version 2, not executed

    listing = client.get(f"/workflows/{workflow_id}/runs", headers=auth_headers).json()

    assert [r["id"] for r in listing] == [newer.id, harness.run.id]  # newest first
    assert [r["workflow_version"] for r in listing] == [2, 1]
    assert listing[0]["assignment_title"] == "Second assignment" and listing[0]["usage"] is None  # nothing has run yet
    older = listing[1]
    assert older["status"] == "node_waiting_for_approval" and older["started_at"] and older["ended_at"] is None
    assert older["usage"]["model_call_count"] == 8 and older["usage"]["cost_is_estimated"] is True
    assert older["workflow_version_id"] == harness.built.published.id
    assert set(older) == {"id", "status", "workflow_version_id", "workflow_version", "task_run_id", "assignment_title", "created_at", "started_at", "ended_at", "usage"}


def test_run_history_limit_and_unknown_workflow(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "e2")
    workflow_id = harness.built.workflow.id
    for _ in range(2):
        task = make_task(db, bootstrap.project)
        db.commit()
        WorkflowExecutionService(db).start_workflow_run_from_task(harness.built.published.id, task.id)
    assert len(client.get(f"/workflows/{workflow_id}/runs", headers=auth_headers).json()) == 3
    assert len(client.get(f"/workflows/{workflow_id}/runs?limit=1", headers=auth_headers).json()) == 1
    assert len(client.get(f"/workflows/{workflow_id}/runs?limit=0", headers=auth_headers).json()) == 1  # clamped, never unbounded
    assert client.get("/workflows/nope/runs", headers=auth_headers).status_code == 404


def test_a_workflow_with_no_runs_has_an_empty_history(client, db, auth_headers, bootstrap):
    workflow = WorkflowDefinitionService(db).create_workflow(bootstrap.project.id, "no-runs")
    assert client.get(f"/workflows/{workflow.id}/runs", headers=auth_headers).json() == []


# =============================================================================
# F. Human Approval through the existing routes
# =============================================================================


def gate(client, auth_headers, run_id):
    node = by_key(detail(client, auth_headers, run_id))["gate"]
    approval = client.get(f"/approvals/{node['approval_id']}", headers=auth_headers).json()
    return SimpleNamespace(id=node["approval_id"], action_fingerprint=approval["action_fingerprint"], approval=approval)


def test_a_human_approves_and_the_run_completes(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "f1")
    approval = gate(client, auth_headers, harness.run.id)
    assert approval.approval["status"] == "pending" and approval.approval["resolved_by"] is None
    before = counts(session_factory)

    response = resolve_via_api(client, auth_headers, approval, approve=True, notes="looks right")

    assert response.status_code == 200 and response.json()["status"] == "approved"
    assert response.json()["resolved_by"] == bootstrap.user.id and response.json()["resolution_note"] == "looks right"
    body = detail(client, auth_headers, harness.run.id)
    assert body["status"] == "completed" and body["ended_at"] is not None
    assert {n["status"] for n in body["nodes"]} == {"completed"}
    assert by_key(body)["gate"]["approval_status"] == "approved"
    assert counts(session_factory) == before  # the decision created nothing (no agent run, task run, job or inference)


def test_the_same_decision_is_an_idempotent_replay_and_the_opposite_is_a_conflict(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "f2")
    approval = gate(client, auth_headers, harness.run.id)
    assert resolve_via_api(client, auth_headers, approval, approve=True).status_code == 200

    replay = resolve_via_api(client, auth_headers, approval, approve=True)
    conflict = resolve_via_api(client, auth_headers, approval, approve=False)

    assert replay.status_code == 200 and replay.json()["status"] == "approved"
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "invalid_state_transition"
    assert detail(client, auth_headers, harness.run.id)["status"] == "completed"  # the conflict changed nothing


def test_a_human_rejects_and_the_run_fails_with_all_evidence_preserved(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(
        db,
        session_factory,
        monkeypatch,
        bootstrap,
        "f3",
        downstream="post",
        findings={node: {"correctness": "NOT_MET", "security": "NOT_MET"} for node in EVALS.values()},
    )
    approval = gate(client, auth_headers, harness.run.id)
    assert approval.approval["status"] == "pending"  # six NOT_MET findings decided nothing
    outputs = {k: n["output_artifact_id"] for k, n in by_key(detail(client, auth_headers, harness.run.id)).items()}

    assert resolve_via_api(client, auth_headers, approval, approve=False, notes="not ready").status_code == 200
    drain(new_worker(session_factory))

    body = detail(client, auth_headers, harness.run.id)
    nodes = by_key(body)
    assert body["status"] == "failed" and nodes["gate"]["status"] == "failed" and nodes["gate"]["approval_status"] == "rejected"
    assert nodes["post"]["status"] == "pending" and "post" not in harness.provider.roles()  # nothing downstream ran
    assert all(nodes[k]["output_artifact_id"] == outputs[k] for k in GATE_PARENTS)  # evidence preserved
    assert nodes["gate"]["failure"] is None  # a human "no" is a decision, not an error


def test_a_stale_fingerprint_is_refused_and_nothing_is_decided(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "f4")
    approval = gate(client, auth_headers, harness.run.id)

    response = resolve_via_api(client, auth_headers, approval, approve=True, fingerprint="0" * 64)

    assert response.status_code == 409 and response.json()["error"]["code"] == "fingerprint_mismatch"
    assert detail(client, auth_headers, harness.run.id)["status"] == "node_waiting_for_approval"
    assert gate(client, auth_headers, harness.run.id).approval["status"] == "pending"


def test_time_restarts_and_reconciliation_never_decide_a_long_waiting_approval(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "f5")
    approval = gate(client, auth_headers, harness.run.id)
    long_ago = datetime.now(timezone.utc) - timedelta(days=60)
    session = session_factory()
    try:
        row = session.get(Approval, approval.id)
        row.requested_at = long_ago
        row.expires_at = long_ago
        session.commit()
    finally:
        session.close()
    for _ in range(3):
        sweep(session_factory)

    body = detail(client, auth_headers, harness.run.id)
    assert body["status"] == "node_waiting_for_approval" and by_key(body)["gate"]["approval_status"] == "pending"
    assert gate(client, auth_headers, harness.run.id).approval["resolved_by"] is None


# =============================================================================
# G. Cancellation
# =============================================================================


def test_cancelling_a_waiting_run_uses_the_existing_semantics(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "g1")
    response = client.post(f"/workflow-runs/{harness.run.id}/cancel", headers=auth_headers)
    assert response.status_code == 200 and response.json() == {"status": "cancellation_requested"}
    for _ in range(3):
        sweep(session_factory)

    body = detail(client, auth_headers, harness.run.id)
    nodes = by_key(body)
    assert body["status"] == "cancelled" and body["ended_at"] is not None and body["cancellation_requested_at"] is not None
    assert nodes["gate"]["status"] == "cancelled" and nodes["gate"]["approval_status"] == "expired"  # a cancel is not a decision
    assert nodes["result"]["status"] == "cancelled"
    assert {nodes[k]["status"] for k in AGENT_NODES} == {"completed"}  # finished work is preserved
    again = client.post(f"/workflow-runs/{harness.run.id}/cancel", headers=auth_headers)
    assert again.status_code == 200  # idempotent


# =============================================================================
# H. Authorization and isolation
# =============================================================================


def test_unauthenticated_reads_and_mutations_are_401(client, db, session_factory, bootstrap, monkeypatch, artifacts_dir):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "h1")
    approval_id = rows(session_factory, Approval)[0].id
    calls = [
        client.get(f"/workflow-runs/{harness.run.id}/detail"),
        client.get(f"/workflows/{harness.built.workflow.id}/runs"),
        client.post(f"/workflow-runs/{harness.run.id}/cancel"),
        client.post(f"/approvals/{approval_id}/resolve", json={"approve": True, "action_fingerprint": "x"}),
        client.get(f"/approvals/{approval_id}/evidence"),
    ]
    assert [c.status_code for c in calls] == [401] * 5


def test_a_viewer_can_watch_but_cannot_decide_cancel_or_modify(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "h2")
    approval = gate(client, auth_headers, harness.run.id)
    bootstrap.membership.role = ProjectRole.VIEWER
    db.commit()
    before = counts(session_factory)

    assert client.get(f"/workflow-runs/{harness.run.id}/detail", headers=auth_headers).status_code == 200
    assert client.get(f"/workflows/{harness.built.workflow.id}/runs", headers=auth_headers).status_code == 200
    assert client.get(f"/approvals/{approval.id}/evidence", headers=auth_headers).status_code == 200
    denied = [
        resolve_via_api(client, auth_headers, approval, approve=True),
        resolve_via_api(client, auth_headers, approval, approve=False),
        client.post(f"/workflow-runs/{harness.run.id}/cancel", headers=auth_headers),
        client.post(
            f"/workflows/{harness.built.workflow.id}/versions/1/runs", headers=auth_headers, json={"task_id": "x"}
        ),
    ]
    assert [d.status_code for d in denied] == [403] * 4
    assert counts(session_factory) == before
    body = detail(client, auth_headers, harness.run.id)
    assert body["status"] == "node_waiting_for_approval" and by_key(body)["gate"]["approval_status"] == "pending"


def test_another_projects_run_and_evidence_never_leak(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    hostile = foreign_run(db, bootstrap, "h3")
    run_id, workflow_id = hostile.run.id, hostile.workflow.id
    other_node_run = rows(session_factory, WorkflowNodeRun, WorkflowNodeRun.workflow_run_id == run_id)[0]
    agent_run = rows(session_factory, AgentRun)[0]

    denied = [
        client.get(f"/workflow-runs/{run_id}/detail", headers=auth_headers),
        client.get(f"/workflow-runs/{run_id}/nodes", headers=auth_headers),
        client.get(f"/workflows/{workflow_id}/runs", headers=auth_headers),
        client.post(f"/workflow-runs/{run_id}/cancel", headers=auth_headers),
        client.get(f"/agent-runs/{agent_run.id}", headers=auth_headers),
    ]
    assert [d.status_code for d in denied] == [403] * 5
    for response in denied:
        assert hostile.task.title not in response.text and workflow_id not in response.text and other_node_run.id not in response.text
    untouched = rows(session_factory, WorkflowRun, WorkflowRun.id == run_id)[0]
    assert untouched.status == WorkflowRunStatus.RUNNING and untouched.cancellation_requested_at is None  # the cancel was refused


def test_a_foreign_approval_artifact_and_evaluation_are_not_readable(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "h4")
    body = detail(client, auth_headers, harness.run.id)
    nodes = by_key(body)
    approval_id, artifact_id, evaluation_id = nodes["gate"]["approval_id"], nodes["code"]["output_artifact_id"], nodes["eval_code"]["evaluation_run_id"]
    # the same rows, seen by a user who is NOT a member of their project
    db.delete(bootstrap.membership)
    db.commit()

    for path in (f"/approvals/{approval_id}", f"/approvals/{approval_id}/evidence", f"/artifacts/{artifact_id}/content", f"/evaluation-runs/{evaluation_id}", f"/workflow-runs/{harness.run.id}/detail"):
        response = client.get(path, headers=auth_headers)
        assert response.status_code == 403, path
        assert "OUTPUT-OF" not in response.text
    resolved = client.post(f"/approvals/{approval_id}/resolve", headers=auth_headers, json={"approve": True, "action_fingerprint": "x"})
    assert resolved.status_code == 403


def test_user_controlled_names_come_back_as_plain_text_data(client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir):
    harness = flagship(db, session_factory, monkeypatch, bootstrap, "h5")
    hostile = '<img src=x onerror="alert(1)">'
    task = session_factory()
    try:
        row = task.get(Task, harness.built.task_run.task_id)
        row.title = hostile
        task.commit()
    finally:
        task.close()
    body = detail(client, auth_headers, harness.run.id)
    assert body["assignment_title"] == hostile  # verbatim data: the client renders it as text


# =============================================================================
# I. No score / rank / winner / verdict / recommendation
# =============================================================================


def keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from keys(inner)


def test_no_read_model_carries_a_score_rank_winner_verdict_or_recommendation(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = flagship(
        db, session_factory, monkeypatch, bootstrap, "i1", findings={"eval_test": {"correctness": "NOT_MET"}}
    )
    forbidden = ("score", "rank", "winner", "recommend", "verdict", "best", "overall", "aggregate", "auto_approve")
    payloads = [
        detail(client, auth_headers, harness.run.id),
        client.get(f"/workflows/{harness.built.workflow.id}/runs", headers=auth_headers).json(),
    ]
    for payload in payloads:
        assert not [k for k in keys(payload) if any(word in k.lower() for word in forbidden)], payload

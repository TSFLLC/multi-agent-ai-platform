"""MA7.5C -- final UAT, hardening and freeze evidence for the MA7.5 product path.

    Planner -> Engineer -> { Test, Security, Code }
    each reviewer -> its own EVALUATION node
    all six -> Human Approval -> Terminal

MA7.5B proved the pieces compose (mostly with hand-driven state for speed and
determinism). This file is the FINAL pass over the REAL platform path -- real
WorkflowDefinition/Version publishing, WorkflowExecutionService, durable job
queue, ``Worker(concurrency=3)``, AgentExecutionService, MA6 evaluation engine,
ApprovalService, reconciliation, artifacts, accounting and read models. Only the
provider network boundary is a deterministic fake.

Adds what earlier slices did not pin end to end:

A. Flagship approve path: exactly-once execution, the barrier, six-entry
   evidence, terminal completion, AND -- before MA7.6 builds UI on it --
   per-run token/cost accounting and a workflow total derivable from the rows.
B. Flagship reject path (with a downstream Agent that must never run).
C. Lineage from WorkflowRun to Artifact through both producer and evaluator
   chains, and the absence of any synthetic bundle/aggregate/winner/score.
D. Human authority: only an authorized human can resolve the gate; time,
   restart, reconciliation, workers and evaluator findings cannot.
E. Restart / recovery driven by REAL Workers (abandoned leases, partial
   evaluations, interrupted decisions, cancellation during parallel work).
F. Concurrency bound and lane overlap.
G. Fail-closed / tamper matrix in the six-parent graph.

Every database is a disposable temp file; artifacts/logs go to tmp dirs.
"""

import ast
import hashlib
import inspect
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import app
from app.config import settings
from app.db.enums import (
    AgentRunRole,
    AgentRunStatus,
    ApprovalStatus,
    EvaluationFinding,
    EvaluationRunStatus,
    JobQueueStatus,
    ModelCallStatus,
    PricingClassification,
    ProjectRole,
    TaskRunStatus,
    UsageSourceType,
    VersionStatus,
    WorkflowNodeRunStatus,
    WorkflowRunStatus,
)
from app.domain.pricing import classify_pricing
from app.models.artifacts_eval import Artifact
from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
from app.models.execution import JobQueue, ModelCall
from app.models.governance import Approval, UsageEvent
from app.models.identity import ProjectMembership
from app.models.observability import ExecutionEvent
from app.models.providers import ProviderModel, ProviderModelSnapshot
from app.models.tasks import AgentRun, AgentRunAttempt, TaskRun
from app.models.workflow import WorkflowNodeRun, WorkflowRun
from app.providers.base import InvokeResponse, ProviderInvalidResponseError
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.approval_service import ApprovalService
from app.services.workflow_execution_service import WorkflowExecutionError, WorkflowExecutionService
from app.worker import Worker
from tests.ma7_3b_support import resolve_via_api
from tests.ma7_4b_support import new_worker, running_worker, wait_until
from tests.test_ma7_5a_evaluation_node import CRITERIA, count_events, counts, in_threads, results_of, rows
from tests.test_ma7_5b_parallel_evaluations import (
    EVALS,
    FINAL_COUNTS,
    GATE_PARENTS,
    REVIEWERS,
    artifact_path,
    build_parallel,
    decide_then_die,
    drain,
    evaluation_by_node,
    expected_fingerprint,
    get_evidence,
    go,
    manual_complete,
    manual_finish_only,
    manual_to_gate,
    product_hooks,
    solo_until_reviewers_done,
    state_of,
    sweep,
)

MTOK = Decimal(1_000_000)
CENT_OF_A_CENT = Decimal("0.0000001")  # comparison tolerance for Numeric(18, 8) sums
AGENT_NODES = ("planner", "engineer", *REVIEWERS, *EVALS.values())

# Distinct per role, so any mis-attribution (one node's usage booked to another)
# cannot cancel out.
USAGE = {
    "planner": (100, 40),
    "engineer": (200, 80),
    "test": (300, 120),
    "security": (400, 160),
    "code": (500, 200),
    "eval_test": (60, 30),
    "eval_security": (70, 35),
    "eval_code": (80, 45),
}
PRICES = {  # (input, output) per million tokens; (0, 0) == verified FREE
    "planner": (0, 0),
    "engineer": (3, 15),
    "test": (1000, 2000),
    "security": (500, 500),
    "code": (250, 750),
    "eval_test": (1000, 1000),
    "eval_security": (200, 400),
    "eval_code": (0, 0),
}
REPORTED = {"security": Decimal("0.12345600")}  # the provider itself reported this actual cost


@pytest.fixture()
def artifacts_dir(monkeypatch, tmp_path):
    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


# -- metering --------------------------------------------------------------------------------


def meter(db, harness):
    """Per-role prices on the provider models (read into each run's pricing
    snapshot at execution) and per-role tokens / one provider-reported cost on
    the fake provider's responses. Wraps the existing fake -- no new provider."""
    for canonical, role in harness.provider.role_of.items():
        model = db.query(ProviderModel).filter(ProviderModel.provider_model_id == canonical).one()
        model.cost_input_per_mtok, model.cost_output_per_mtok = (Decimal(x) for x in PRICES.get(role, (0, 0)))
    db.commit()
    original = harness.provider.invoke

    def invoke(request):
        response = original(request)
        role = harness.provider.role_of[request.provider_model_id]
        tokens_in, tokens_out = USAGE.get(role, (1, 1))
        return InvokeResponse(
            text=response.text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_amount=REPORTED.get(role),
            latency_ms=7,
        )

    harness.provider.invoke = invoke


def expected_cost(role):
    """(amount, is_estimated) exactly as the platform's own rules define them:
    provider-reported -> actual; verified FREE -> known zero; else priced estimate."""
    tokens_in, tokens_out = USAGE[role]
    price_in, price_out = (Decimal(x) for x in PRICES[role])
    if role in REPORTED:
        return REPORTED[role], False
    if price_in == 0 and price_out == 0:
        return Decimal(0), False
    return Decimal(tokens_in) / MTOK * price_in + Decimal(tokens_out) / MTOK * price_out, True


def close(a, b):
    return abs(Decimal(str(a)) - Decimal(str(b))) <= CENT_OF_A_CENT


def one(items):
    items = list(items)
    assert len(items) == 1, items
    return items[0]


# -- shared flows ----------------------------------------------------------------------------


def start_flagship(db, session_factory, monkeypatch, bootstrap, label, *, hooks=True, **kwargs):
    harness = build_parallel(db, bootstrap.project, monkeypatch, label, **kwargs)
    meter(db, harness)
    release, errors = product_hooks(harness) if hooks else (None, [])
    go(db, harness)
    return harness, release, errors


def run_to_waiting_gate(session_factory, harness, release):
    release.set()
    assert wait_until(lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL)
    assert wait_until(lambda: {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE})
    return state_of(session_factory, harness)


def model_call_total(session_factory):
    calls = rows(session_factory, ModelCall)
    return (
        len(calls),
        sum(c.tokens_in for c in calls),
        sum(c.tokens_out for c in calls),
        sum((Decimal(str(c.cost_amount)) for c in calls), Decimal(0)),
    )


def assert_accounting(session_factory, harness, *, executed=AGENT_NODES):
    """Per-run and workflow-level accounting, derived only from persisted rows."""
    state = state_of(session_factory, harness)
    run_id = harness.run.id
    snapshots = {s.id: s for s in rows(session_factory, ProviderModelSnapshot)}
    calls = rows(session_factory, ModelCall)
    assert len(calls) == len(executed)  # one inference per executed agent run, none for anything else

    derived_in = derived_out = 0
    derived_cost = Decimal(0)
    for key in executed:
        node_run = state["node_runs"][key]
        agent = one(rows(session_factory, AgentRun, AgentRun.id == node_run.agent_run_id))
        call = one(rows(session_factory, ModelCall, ModelCall.agent_run_id == agent.id))
        attempt = one(rows(session_factory, AgentRunAttempt, AgentRunAttempt.agent_run_id == agent.id))
        snapshot = snapshots[agent.provider_model_snapshot_id]

        # -- provider/model snapshot: the call, the run and the snapshot all agree
        assert agent.status == AgentRunStatus.COMPLETED
        assert call.status == ModelCallStatus.SUCCESS and call.agent_run_attempt_id == attempt.id
        assert call.provider_model_snapshot_id == agent.provider_model_snapshot_id
        assert (call.model_id, call.provider_id) == (agent.model_id, agent.provider_id) == (
            snapshot.model_id,
            snapshot.provider_id,
        )
        price_in, price_out = (Decimal(x) for x in PRICES[key])
        assert (snapshot.pricing_input_per_mtok, snapshot.pricing_output_per_mtok) == (price_in, price_out)  # NOT live
        assert classify_pricing(snapshot.pricing_input_per_mtok, snapshot.pricing_output_per_mtok) == (
            PricingClassification.FREE if price_in == 0 and price_out == 0 else PricingClassification.PAID
        )

        # -- tokens and cost (and its basis: actual vs estimated)
        tokens_in, tokens_out = USAGE[key]
        assert (call.tokens_in, call.tokens_out) == (tokens_in, tokens_out)
        amount, estimated = expected_cost(key)
        assert close(call.cost_amount, amount) and call.cost_is_estimated is estimated
        assert call.cost_currency == "USD"
        derived_in, derived_out, derived_cost = derived_in + call.tokens_in, derived_out + call.tokens_out, derived_cost + amount

        # -- TaskRun association: one TaskRun per agent run, tagged with its node
        task_run = one(rows(session_factory, TaskRun, TaskRun.id == agent.task_run_id))
        assert task_run.config_snapshot["node_key"] == key
        assert task_run.workflow_version_id == harness.run.workflow_version_id
        # -- WorkflowRun / WorkflowNodeRun association: exactly one node run owns this agent run
        owner = one(rows(session_factory, WorkflowNodeRun, WorkflowNodeRun.agent_run_id == agent.id))
        assert owner.id == node_run.id and owner.workflow_run_id == run_id
        if key in EVALS.values():
            assert agent.role == AgentRunRole.EVALUATOR
            assert task_run.config_snapshot["workflow_run_id"] == run_id  # evaluator lineage is also on its TaskRun
            assert task_run.config_snapshot["workflow_node_run_id"] == node_run.id

    # -- the workflow total is exactly derivable: ModelCall -> WorkflowNodeRun.agent_run_id -> workflow_run_id
    session = session_factory()
    try:
        total_in, total_out, total_cost = session.execute(
            select(func.sum(ModelCall.tokens_in), func.sum(ModelCall.tokens_out), func.sum(ModelCall.cost_amount))
            .join(WorkflowNodeRun, WorkflowNodeRun.agent_run_id == ModelCall.agent_run_id)
            .where(WorkflowNodeRun.workflow_run_id == run_id)
        ).one()
    finally:
        session.close()
    assert (total_in, total_out) == (derived_in, derived_out) == (
        sum(USAGE[k][0] for k in executed),
        sum(USAGE[k][1] for k in executed),
    )
    assert close(total_cost, derived_cost)
    assert (len(calls), *model_call_total(session_factory)[1:3]) == (len(executed), derived_in, derived_out)  # nothing outside the workflow

    # -- the project ledger and the Flight Recorder agree with the ModelCall rows
    usage = rows(session_factory, UsageEvent)
    assert {u.source_type for u in usage} == {UsageSourceType.MODEL_CALL}
    assert sorted(u.source_ref_id for u in usage) == sorted(c.id for c in calls)
    assert close(sum((Decimal(str(u.amount)) for u in usage), Decimal(0)), derived_cost)
    recorded = rows(session_factory, ExecutionEvent, ExecutionEvent.event_type == "model_call.completed")
    assert len(recorded) == len(executed)
    assert close(sum((Decimal(str(e.cost)) for e in recorded), Decimal(0)), derived_cost)
    assert sum(e.tokens_in for e in recorded) == derived_in and sum(e.tokens_out for e in recorded) == derived_out
    return SimpleNamespace(tokens_in=derived_in, tokens_out=derived_out, cost=derived_cost)


def assert_evaluation_usage_through_the_read_model(client, auth_headers, session_factory, harness):
    """The MA6 evaluation read model already aggregates each evaluator's usage."""
    for node, evaluation in evaluation_by_node(session_factory, harness).items():
        body = client.get(f"/evaluation-runs/{evaluation.id}", headers=auth_headers).json()
        evidence = body["evaluator"]["execution_evidence"]
        tokens_in, tokens_out = USAGE[node]
        amount, estimated = expected_cost(node)
        assert (evidence["tokens_in"], evidence["tokens_out"], evidence["total_tokens"]) == (
            tokens_in,
            tokens_out,
            tokens_in + tokens_out,
        )
        assert close(evidence["cost_amount"], amount) and evidence["cost_is_estimated"] is estimated


def assert_lineage(client, auth_headers, session_factory, harness):
    """WorkflowRun -> node run -> TaskRun -> AgentRun -> Artifact for the six
    producers/evaluators, and Evaluation node -> EvaluationRun -> subject +
    evaluator -> CriterionResults; then all six -> the Approval's evidence."""
    state = state_of(session_factory, harness)
    run_id = harness.run.id
    nodes_read = {n["id"]: n for n in client.get(f"/workflow-runs/{run_id}/nodes", headers=auth_headers).json()}

    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    artifacts = {}
    for key in AGENT_NODES:
        node_run = state["node_runs"][key]
        assert nodes_read[node_run.id]["agent_run_id"] == node_run.agent_run_id  # the read model says the same
        agent = one(rows(session_factory, AgentRun, AgentRun.id == node_run.agent_run_id))
        artifact = one(rows(session_factory, Artifact, Artifact.agent_run_id == agent.id))  # one output, once
        assert node_run.output_snapshot_ref == artifact.id
        assert artifact.content_hash == sha(artifact.storage_ref)  # file bytes still match the recorded hash
        artifacts[key] = artifact

    evaluations = evaluation_by_node(session_factory, harness)
    for reviewer, node in EVALS.items():
        evaluation = evaluations[node]
        detail = client.get(f"/evaluation-runs/{evaluation.id}", headers=auth_headers).json()
        assert nodes_read[state["node_runs"][node].id]["evaluation_run_id"] == evaluation.id
        # subject: the ONE reviewer AgentRun / artifact / exact bytes
        assert evaluation.subject_agent_run_id == state["node_runs"][reviewer].agent_run_id
        assert evaluation.subject_artifact_id == artifacts[reviewer].id
        assert evaluation.subject_artifact_content_hash == artifacts[reviewer].content_hash == sha(artifacts[reviewer].storage_ref)
        assert detail["subject"]["agent_run_id"] == evaluation.subject_agent_run_id
        assert detail["subject"]["artifact_id"] == artifacts[reviewer].id
        assert detail["subject"]["artifact_content_hash"] == artifacts[reviewer].content_hash
        # evaluator: its own AgentRun / TaskRun / artifact, never the reviewer's
        assert evaluation.evaluator_agent_run_id == state["node_runs"][node].agent_run_id != evaluation.subject_agent_run_id
        evaluator_agent = one(rows(session_factory, AgentRun, AgentRun.id == evaluation.evaluator_agent_run_id))
        assert evaluator_agent.task_run_id == evaluation.evaluator_task_run_id
        assert detail["evaluator"]["agent_run_id"] == evaluation.evaluator_agent_run_id
        assert state["node_runs"][node].output_snapshot_ref == artifacts[node].id != artifacts[reviewer].id
        results = rows(
            session_factory, EvaluationCriterionResult, EvaluationCriterionResult.evaluation_run_id == evaluation.id
        )
        assert sorted(r.criterion_key for r in results) == sorted(CRITERIA)

    # all six -> the one Approval's evidence
    approval = one(state["approvals"])
    gate = state["node_runs"]["gate"]
    assert approval.scope_ref_id == gate.id and nodes_read[gate.id]["approval_id"] == approval.id
    evidence = get_evidence(client, auth_headers, approval.id).json()
    assert [e["node_key"] for e in evidence["evidence"]] == GATE_PARENTS
    for entry in evidence["evidence"]:
        assert entry["node_run_id"] == state["node_runs"][entry["node_key"]].id
        assert entry["artifact_id"] == artifacts[entry["node_key"]].id
        assert entry["content_hash"] == artifacts[entry["node_key"]].content_hash and entry["content_intact"] is True

    # -- nothing synthetic: no bundle/JOIN/aggregate artifact, no gate or terminal agent, no decision-shaped field
    assert len(rows(session_factory, Artifact)) == len(AGENT_NODES) == len({a.id for a in artifacts.values()})
    assert gate.agent_run_id is None and gate.output_snapshot_ref is None  # a multi-parent gate has no output artifact
    assert state["node_runs"]["result"].agent_run_id is None
    forbidden = ("score", "rank", "winner", "recommend", "aggregate", "bundle", "verdict", "best", "overall")

    def keys(value):
        if isinstance(value, dict):
            for key, inner in value.items():
                yield key
                yield from keys(inner)
        elif isinstance(value, list):
            for inner in value:
                yield from keys(inner)

    payloads = [evidence, list(nodes_read.values())] + [
        client.get(f"/evaluation-runs/{e.id}", headers=auth_headers).json() for e in evaluations.values()
    ]
    for payload in payloads:
        assert not [k for k in keys(payload) if any(word in k.lower() for word in forbidden)], payload


def gate_effects_snapshot(session_factory, harness):
    return (
        counts(session_factory),
        model_call_total(session_factory),
        len(harness.provider.calls),
        len(rows(session_factory, AgentRunAttempt)),
    )


# =============================================================================
# A. Flagship approve path -- real Worker(concurrency=3), real everything but the provider
# =============================================================================


def test_flagship_approve_path_with_accounting_and_lineage(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness, release, errors = start_flagship(
        db,
        session_factory,
        monkeypatch,
        bootstrap,
        "fa",
        findings={"eval_security": {"security": "NOT_MET"}, "eval_code": {"correctness": "PARTIAL"}},
    )
    parent = harness.built.task_run.id
    with running_worker(session_factory, 3) as the_worker:
        state = run_to_waiting_gate(session_factory, harness, release)
        assert errors == []  # three reviewers, then three evaluators, were each in the provider AT THE SAME TIME

        # exactly-once execution: each agent node ran once; planner and engineer strictly first
        assert sorted(harness.provider.roles()) == sorted(AGENT_NODES)
        assert harness.provider.roles()[:2] == ["planner", "engineer"]
        assert counts(session_factory) == FINAL_COUNTS

        # three lanes of the one worker ran the reviewers, and three ran the evaluators, concurrently
        for group in (REVIEWERS, tuple(EVALS.values())):
            agent_ids = [state["node_runs"][k].agent_run_id for k in group]
            lanes = {
                a.worker_id for a in rows(session_factory, AgentRunAttempt, AgentRunAttempt.agent_run_id.in_(agent_ids))
            }
            assert len(lanes) == 3 and all(lane.startswith(the_worker.worker_id + ":lane") for lane in lanes)

        # the gate opened only after all six parents; one PENDING approval; six-entry ordered intact evidence
        assert {state["nodes"][k] for k in GATE_PARENTS} == {WorkflowNodeRunStatus.COMPLETED}
        approval = one(state["approvals"])
        assert approval.status == ApprovalStatus.PENDING and approval.resolved_by is None
        assert approval.action_fingerprint == expected_fingerprint(session_factory, harness)[0]
        assert count_events(session_factory, parent, "approval.requested") == 1

        # findings are evidence, persisted per evaluation; none of them decided anything
        evaluations = evaluation_by_node(session_factory, harness)
        assert results_of(session_factory, evaluations["eval_security"].id)["security"] == EvaluationFinding.NOT_MET
        assert results_of(session_factory, evaluations["eval_code"].id)["correctness"] == EvaluationFinding.PARTIAL
        assert {e.status for e in evaluations.values()} == {EvaluationRunStatus.COMPLETED}

        # ---- accounting, lineage and the gate's zero-inference guarantee, BEFORE the human acts
        totals = assert_accounting(session_factory, harness)
        assert_evaluation_usage_through_the_read_model(client, auth_headers, session_factory, harness)
        assert_lineage(client, auth_headers, session_factory, harness)
        before_decision = gate_effects_snapshot(session_factory, harness)
        assert one(rows(session_factory, TaskRun, TaskRun.id == harness.built.task_run.id)).id == parent  # the gate made no TaskRun

        # ---- the human approves; the terminal work finishes; the decision itself cost/created nothing
        assert resolve_via_api(client, auth_headers, approval).status_code == 200
        final = state_of(session_factory, harness)
        assert final["run"] == WorkflowRunStatus.COMPLETED and final["run_ended_at"] is not None
        assert set(final["nodes"].values()) == {WorkflowNodeRunStatus.COMPLETED}
        assert final["approvals"][0].status == ApprovalStatus.APPROVED and final["approvals"][0].resolved_by == bootstrap.user.id
        assert gate_effects_snapshot(session_factory, harness) == before_decision
        assert len(harness.provider.calls) == 8  # the gate and the terminal node never touched the provider

    # accounting is unchanged by the decision; restart/reconciliation change nothing, ever
    assert assert_accounting(session_factory, harness).cost == totals.cost
    before = (counts(session_factory), state_of(session_factory, harness)["nodes"])
    for _ in range(3):
        sweep(session_factory)
    in_threads(4, lambda _i: sweep(session_factory))
    assert (counts(session_factory), state_of(session_factory, harness)["nodes"]) == before
    assert counts(session_factory) == FINAL_COUNTS
    assert count_events(session_factory, parent, "workflow.completed") == 1
    assert len(rows(session_factory, Approval)) == 1


def test_the_workflow_total_is_the_sum_of_the_runs_and_excludes_other_workflow_runs(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    """Two runs of the same published workflow share the database; a total scoped
    by workflow_run_id only ever contains its own runs' usage."""
    first, _release, _errors = start_flagship(db, session_factory, monkeypatch, bootstrap, "wt", hooks=False)
    drain(new_worker(session_factory))
    assert state_of(session_factory, first)["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert model_call_total(session_factory)[0] == 8

    other_task_run = TaskRun(
        task_id=first.built.task_run.task_id, status=TaskRunStatus.CREATED, created_at=datetime.now(timezone.utc)
    )
    db.add(other_task_run)
    db.commit()
    second_run = WorkflowExecutionService(db).start_workflow_run(first.built.published.id, other_task_run.id)
    drain(new_worker(session_factory))

    session = session_factory()
    try:
        per_run = {
            run.id: session.execute(
                select(func.count(), func.sum(ModelCall.tokens_in), func.sum(ModelCall.tokens_out))
                .select_from(ModelCall)
                .join(WorkflowNodeRun, WorkflowNodeRun.agent_run_id == ModelCall.agent_run_id)
                .where(WorkflowNodeRun.workflow_run_id == run.id)
            ).one()
            for run in (first.run, second_run)
        }
    finally:
        session.close()
    expected = (8, sum(u[0] for u in USAGE.values()), sum(u[1] for u in USAGE.values()))
    assert model_call_total(session_factory)[0] == 16
    assert per_run[first.run.id] == expected and per_run[second_run.id] == expected


# =============================================================================
# B. Flagship reject path
# =============================================================================


def test_flagship_reject_path_not_met_never_rejects_and_preserves_everything(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness, release, errors = start_flagship(
        db,
        session_factory,
        monkeypatch,
        bootstrap,
        "fr",
        downstream="post",
        findings={node: {"correctness": "NOT_MET", "security": "NOT_MET"} for node in EVALS.values()},
    )
    with running_worker(session_factory, 3):
        state = run_to_waiting_gate(session_factory, harness, release)
        assert errors == []
        approval = one(state["approvals"])
        outputs = {k: state["node_runs"][k].output_snapshot_ref for k in GATE_PARENTS}
        # six evaluators-worth of NOT_MET decided nothing
        assert {r.finding for e in evaluation_by_node(session_factory, harness).values() for r in rows(
            session_factory, EvaluationCriterionResult, EvaluationCriterionResult.evaluation_run_id == e.id
        )} == {EvaluationFinding.NOT_MET}
        assert approval.status == ApprovalStatus.PENDING and state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
        totals_before = assert_accounting(session_factory, harness)
        before = gate_effects_snapshot(session_factory, harness)

        assert resolve_via_api(client, auth_headers, approval, approve=False, notes="not good enough").status_code == 200
        assert wait_until(lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.FAILED)

    final = state_of(session_factory, harness)
    assert final["run_ended_at"] is not None and final["nodes"]["gate"] == WorkflowNodeRunStatus.FAILED
    assert final["nodes"]["post"] == WorkflowNodeRunStatus.PENDING and final["nodes"]["result"] == WorkflowNodeRunStatus.PENDING
    assert "post" not in harness.provider.roles()  # downstream work never executed
    rejected = one(final["approvals"])
    assert rejected.status == ApprovalStatus.REJECTED and rejected.resolved_by == bootstrap.user.id and rejected.resolved_at
    for key in GATE_PARENTS:  # every completed piece of evidence is preserved, byte for byte
        assert final["nodes"][key] == WorkflowNodeRunStatus.COMPLETED and final["node_runs"][key].output_snapshot_ref == outputs[key]
    assert {e.status for e in rows(session_factory, EvaluationRun)} == {EvaluationRunStatus.COMPLETED}
    assert gate_effects_snapshot(session_factory, harness) == before  # rejection created no run, call or cost
    assert assert_accounting(session_factory, harness).cost == totals_before.cost

    assert resolve_via_api(client, auth_headers, rejected, approve=False).status_code == 200  # idempotent replay
    assert resolve_via_api(client, auth_headers, rejected, approve=True).status_code == 409  # opposite conflicts
    assert one(state_of(session_factory, harness)["approvals"]).status == ApprovalStatus.REJECTED
    for _ in range(3):
        sweep(session_factory)
    assert state_of(session_factory, harness)["run"] == WorkflowRunStatus.FAILED
    assert gate_effects_snapshot(session_factory, harness) == before


def test_an_approved_downstream_agent_executes_exactly_once(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness, release, errors = start_flagship(db, session_factory, monkeypatch, bootstrap, "fd", downstream="post")
    with running_worker(session_factory, 3):
        state = run_to_waiting_gate(session_factory, harness, release)
        assert harness.provider.count("post") == 0
        assert resolve_via_api(client, auth_headers, one(state["approvals"])).status_code == 200
        assert wait_until(lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.COMPLETED)
    assert errors == [] and harness.provider.count("post") == 1  # once
    final = counts(session_factory)
    assert final["agent_runs"] == 9 and final["task_runs"] == 10 and final["approvals"] == 1
    for _ in range(3):
        sweep(session_factory)
    in_threads(4, lambda _i: sweep(session_factory))
    assert counts(session_factory) == final and harness.provider.count("post") == 1


# =============================================================================
# C. Human authority
# =============================================================================


def test_only_an_authorized_human_can_resolve_the_real_gate(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "ha")
    approval = manual_to_gate(db, session_factory, harness, tmp_path)["approvals"][0]
    membership = db.query(ProjectMembership).filter(
        ProjectMembership.project_id == bootstrap.project.id, ProjectMembership.user_id == bootstrap.user.id
    ).one()
    original_role = membership.role
    before = counts(session_factory)

    body = {"approve": True, "action_fingerprint": approval.action_fingerprint}
    assert client.post(f"/approvals/{approval.id}/resolve", json=body).status_code == 401  # nobody at all
    assert client.post(f"/approvals/{approval.id}/resolve", headers={"Authorization": "Bearer nonsense"}, json=body).status_code == 401
    membership.role = ProjectRole.VIEWER
    db.commit()
    viewer = resolve_via_api(client, auth_headers, approval)
    assert viewer.status_code == 403 and viewer.json()["error"]["code"] == "forbidden"  # read-only role cannot decide
    unchanged = state_of(session_factory, harness)
    assert one(unchanged["approvals"]).status == ApprovalStatus.PENDING and one(unchanged["approvals"]).resolved_by is None
    assert unchanged["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL and counts(session_factory) == before

    membership.role = original_role
    db.commit()
    assert resolve_via_api(client, auth_headers, approval).status_code == 200  # the authorized human can
    done = state_of(session_factory, harness)
    assert done["run"] == WorkflowRunStatus.COMPLETED and one(done["approvals"]).resolved_by == bootstrap.user.id


def test_time_restart_reconciliation_workers_and_findings_cannot_decide_the_gate(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness, release, errors = start_flagship(
        db,
        session_factory,
        monkeypatch,
        bootstrap,
        "hb",
        findings={node: {"correctness": "MET", "security": "MET"} for node in EVALS.values()},  # even all-MET decides nothing
    )
    with running_worker(session_factory, 3):
        state = run_to_waiting_gate(session_factory, harness, release)
    approval = one(state["approvals"])
    before = counts(session_factory)
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)

    # time: everything that could time out is 30 days old, and the approval itself is long "expired"
    session = session_factory()
    try:
        session.get(Approval, approval.id).expires_at = long_ago
        session.get(Approval, approval.id).requested_at = long_ago
        for model in (AgentRun, TaskRun):
            for row in session.query(model).all():
                row.created_at = long_ago
        session.commit()
    finally:
        session.close()

    # restart + reconciliation + idle workers: several full worker lifetimes, plus direct sweeps and polls
    for _ in range(3):
        with running_worker(session_factory, 3):
            assert wait_until(lambda: False, timeout=0.3) is False
        sweep(session_factory)
    idle = new_worker(session_factory)
    assert idle.run_once() is False
    in_threads(4, lambda _i: sweep(session_factory))

    after = state_of(session_factory, harness)
    still = one(after["approvals"])
    assert still.status == ApprovalStatus.PENDING and still.resolved_by is None and still.resolved_at is None
    assert after["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL and after["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert counts(session_factory) == before and errors == []
    assert resolve_via_api(client, auth_headers, still).status_code == 200  # only the human moves it
    assert state_of(session_factory, harness)["run"] == WorkflowRunStatus.COMPLETED


def test_no_code_outside_the_human_resolve_path_can_write_an_approval_decision():
    """Structural guarantee over the WHOLE application: APPROVED/REJECTED are
    only ever *compared* outside ApprovalService, ``resolve`` is called only by
    HTTP callers, and no other module updates Approval rows or assigns a status."""
    root = Path(inspect.getfile(app)).parent
    decisions = {"APPROVED", "REJECTED"}
    offenders = []
    resolve_callers = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        relative = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in decisions
                and isinstance(node.value, ast.Name)
                and node.value.id == "ApprovalStatus"
                and relative != "services/approval_service.py"
            ):
                cursor = node
                while isinstance(parents.get(cursor), (ast.Tuple, ast.List, ast.Set)):
                    cursor = parents[cursor]
                if not isinstance(parents.get(cursor), ast.Compare):
                    offenders.append((relative, node.lineno))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "resolve"
                and {"approve", "action_fingerprint"} & {k.arg for k in node.keywords}
            ):
                resolve_callers.append(relative)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "update"
                and any(isinstance(a, ast.Name) and a.id == "Approval" for a in node.args)
                and relative != "services/approval_service.py"
            ):
                offenders.append((relative, node.lineno))
    assert offenders == []
    assert set(resolve_callers) <= {"api/routers/approvals.py"}  # the authenticated HTTP endpoint only


def test_the_approval_node_creates_no_task_run_agent_run_or_model_call(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "hd")
    state = manual_to_gate(db, session_factory, harness, tmp_path)  # a gate reached, then decided by a human
    gate_run = state["node_runs"]["gate"]
    assert gate_run.agent_run_id is None
    with_gate = counts(session_factory)
    with_gate_calls = len(harness.provider.calls)
    assert len(rows(session_factory, ModelCall)) == 0 and with_gate_calls == 0  # manual mode: not one inference anywhere
    for _ in range(3):
        sweep(session_factory)
    assert counts(session_factory) == with_gate and len(harness.provider.calls) == 0
    session = session_factory()
    try:
        assert ApprovalService(session).get(state["approvals"][0].id).status == ApprovalStatus.PENDING
    finally:
        session.close()


# =============================================================================
# D. No score / rank / winner anywhere in the schema
# =============================================================================


def test_no_score_rank_winner_or_recommendation_column_exists_on_any_ma75_row():
    forbidden = ("score", "rank", "winner", "recommend", "aggregate", "verdict", "best", "overall")
    for model in (EvaluationRun, EvaluationCriterionResult, Approval, WorkflowNodeRun, WorkflowRun):
        columns = [c.name for c in model.__table__.columns]
        assert not [c for c in columns if any(word in c.lower() for word in forbidden)], (model.__name__, columns)


# =============================================================================
# E. Restart / recovery driven by REAL Workers
# =============================================================================


@pytest.mark.parametrize("evaluations_done_before_the_crash", [0, 1, 2])
def test_a_fresh_worker_finishes_a_run_whose_first_worker_died_with_evaluations_queued_or_partial(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir, evaluations_done_before_the_crash
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, f"ra{evaluations_done_before_the_crash}")
    solo = solo_until_reviewers_done(db, session_factory, harness)  # three evaluations dispatched and QUEUED
    for _ in range(evaluations_done_before_the_crash):
        assert solo.run_once() is True
    del solo  # ... and the process is gone
    mid = state_of(session_factory, harness)
    assert mid["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and mid["approvals"] == []

    with running_worker(session_factory, 3):
        assert wait_until(lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL)
        assert wait_until(lambda: {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE})

    assert counts(session_factory) == FINAL_COUNTS
    assert sorted(harness.provider.roles()) == sorted(AGENT_NODES)  # every role inferred exactly once across both workers
    approval = one(state_of(session_factory, harness)["approvals"])
    assert approval.action_fingerprint == expected_fingerprint(session_factory, harness)[0]
    assert resolve_via_api(client, auth_headers, approval).status_code == 200
    assert state_of(session_factory, harness)["run"] == WorkflowRunStatus.COMPLETED


def test_workers_that_died_during_the_reviewer_fan_out_are_reclaimed_after_their_leases_lapse(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "rb")
    go(db, harness)
    solo = new_worker(session_factory)
    assert solo.run_once() is True and solo.run_once() is True  # planner, engineer -> the three reviewers are queued
    abandoned = []
    session = session_factory()
    try:
        for dead in ("dead-worker-a", "dead-worker-b"):  # two workers claim a reviewer job each, then die
            job = JobQueueRepository().claim_one(session, worker_id=dead, lease_seconds=30)
            assert job is not None
            job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            abandoned.append(job.id)
        session.commit()
    finally:
        session.close()

    with running_worker(session_factory, 3):
        assert wait_until(lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL)
        assert wait_until(lambda: {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE})

    reclaimed = rows(session_factory, JobQueue, JobQueue.id.in_(abandoned))
    assert {j.attempt_count for j in reclaimed} == {2}  # one abandoned attempt + one real execution
    assert counts(session_factory) == FINAL_COUNTS
    assert sorted(harness.provider.roles()) == sorted(AGENT_NODES)  # the abandoned jobs' inference happened once
    assert len(rows(session_factory, Approval)) == 1


def real_run_to_gate(db, session_factory, harness):
    """Reaches the waiting gate through real execution: every job ran and is DONE
    (a hand-completed run would leave its queue job behind for the next worker)."""
    go(db, harness)
    drain(new_worker(session_factory))
    state = state_of(session_factory, harness)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE}
    return state


def test_an_interrupted_approval_is_completed_by_a_real_worker_without_a_human_or_duplicate_work(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "rc", downstream="post")
    state = real_run_to_gate(db, session_factory, harness)
    decide_then_die(session_factory, bootstrap, state["approvals"][0], monkeypatch)  # decided; the process died
    assert state_of(session_factory, harness)["nodes"]["post"] == WorkflowNodeRunStatus.PENDING
    before = counts(session_factory)

    with running_worker(session_factory, 3):  # the restarted worker finishes the job -- nobody decides again
        assert wait_until(lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.COMPLETED)

    assert harness.provider.count("post") == 1 and len(harness.provider.calls) == 9  # 8 upstream once + post once
    assert {k: v - before[k] for k, v in counts(session_factory).items()} == {
        "tasks": 0, "task_runs": 1, "agent_runs": 1, "evaluation_runs": 0, "jobs": 1, "criterion_results": 0, "approvals": 0,
    }
    assert one(state_of(session_factory, harness)["approvals"]).resolved_by == bootstrap.user.id  # still the human's decision


def test_an_interrupted_rejection_is_completed_by_a_real_worker_and_runs_no_downstream_work(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "rd", downstream="post")
    state = real_run_to_gate(db, session_factory, harness)
    decide_then_die(session_factory, bootstrap, state["approvals"][0], monkeypatch, approve=False)
    assert state_of(session_factory, harness)["run"] == WorkflowRunStatus.RUNNING
    before = counts(session_factory)

    with running_worker(session_factory, 3):
        assert wait_until(lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.FAILED)

    final = state_of(session_factory, harness)
    assert final["nodes"]["post"] == WorkflowNodeRunStatus.PENDING
    assert sorted(harness.provider.roles()) == sorted(AGENT_NODES)  # nothing ran after the rejection
    assert {final["nodes"][k] for k in GATE_PARENTS} == {WorkflowNodeRunStatus.COMPLETED}
    assert counts(session_factory) == before
    assert count_events(session_factory, harness.built.task_run.id, "workflow.failed") == 1


def test_cancelling_during_the_parallel_reviewers_stops_everything_cleanly_under_a_real_worker(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "re")
    in_flight = {node: threading.Event() for node in REVIEWERS}
    release = threading.Event()

    def hold(node):
        def hook(_request):
            in_flight[node].set()
            assert release.wait(30)

        return hook

    for node in REVIEWERS:
        harness.provider.hooks[node] = hold(node)
    go(db, harness)
    with running_worker(session_factory, 3):
        assert wait_until(lambda: all(e.is_set() for e in in_flight.values()))  # all three reviewers mid-call
        WorkflowExecutionService(db).cancel_workflow_run(harness.run.id, cancelled_by_user_id=bootstrap.user.id)
        release.set()
        assert wait_until(lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.CANCELLED)
        assert wait_until(lambda: not {j.status for j in rows(session_factory, JobQueue)} & {JobQueueStatus.PENDING, JobQueueStatus.LEASED})

    state = state_of(session_factory, harness)
    assert not any(role in harness.provider.roles() for role in EVALS.values())  # nothing evaluated a cancelled reviewer
    assert rows(session_factory, EvaluationRun) == [] and state["approvals"] == []
    assert state["nodes"]["gate"] in (WorkflowNodeRunStatus.CANCELLED, WorkflowNodeRunStatus.PENDING)
    assert not {a.status for a in rows(session_factory, AgentRun)} & {
        AgentRunStatus.RUNNING,
        AgentRunStatus.CANCELLING,
        AgentRunStatus.WAITING_FOR_MODEL,
    }
    for _ in range(3):
        sweep(session_factory)
    assert state_of(session_factory, harness)["run"] == WorkflowRunStatus.CANCELLED


# =============================================================================
# F. Concurrency bound
# =============================================================================


@pytest.mark.parametrize("bad", [0, 5, 64])
def test_the_supported_worker_concurrency_bound_is_unchanged(session_factory, bad):
    with pytest.raises(ValueError, match="concurrency must be between 1 and 4"):
        Worker(session_factory=session_factory, concurrency=bad)
    assert Worker(session_factory=session_factory, concurrency=4).concurrency == 4


# =============================================================================
# G. Fail-closed and tamper matrix in the six-parent graph
# =============================================================================


def assert_fail_closed(session_factory, harness):
    """A failed run stays failed: no gate, no approval, nothing left dangling,
    and no amount of reconciliation 'recovers' corrupted or failed evidence."""
    state = state_of(session_factory, harness)
    assert state["run"] == WorkflowRunStatus.FAILED and state["run_ended_at"] is not None
    assert state["approvals"] == []
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and state["nodes"]["result"] == WorkflowNodeRunStatus.PENDING
    assert not {e.status for e in rows(session_factory, EvaluationRun)} & {EvaluationRunStatus.RUNNING, EvaluationRunStatus.PENDING}
    assert not {j.status for j in rows(session_factory, JobQueue)} & {JobQueueStatus.PENDING, JobQueueStatus.LEASED}
    before = (counts(session_factory), state["nodes"], len(harness.provider.calls))
    for _ in range(3):
        sweep(session_factory)
    in_threads(4, lambda _i: sweep(session_factory))
    again = state_of(session_factory, harness)
    assert (counts(session_factory), again["nodes"], len(harness.provider.calls)) == before
    assert again["run"] == WorkflowRunStatus.FAILED and again["approvals"] == []
    return state


@pytest.mark.parametrize(
    "failing_role,mode",
    [
        ("security", "provider_error"),  # a reviewer fails
        ("eval_security", "provider_error"),  # an evaluator's provider call fails
        ("eval_code", "malformed"),  # an evaluator returns something that is not a valid evaluation
    ],
)
def test_reviewer_and_evaluator_failures_fail_the_run_closed(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, failing_role, mode
):
    texts = {failing_role: "this is not json at all"} if mode == "malformed" else None
    harness = build_parallel(db, bootstrap.project, monkeypatch, f"ga-{failing_role}", texts=texts)
    if mode == "provider_error":
        harness.provider.hooks[failing_role] = lambda _r: (_ for _ in ()).throw(ProviderInvalidResponseError("boom"))
    go(db, harness)
    drain(new_worker(session_factory))

    state = assert_fail_closed(session_factory, harness)
    assert state["nodes"][failing_role] == WorkflowNodeRunStatus.FAILED
    if failing_role in EVALS.values():
        failed = evaluation_by_node(session_factory, harness)[failing_role]
        assert failed.status == EvaluationRunStatus.FAILED and results_of(session_factory, failed.id) == {}  # no findings kept


@pytest.mark.parametrize("tamper", ["delete_file", "alter_bytes", "alter_recorded_hash"])
def test_a_tampered_or_missing_reviewer_artifact_stops_its_evaluation_before_any_inference(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path, tamper
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, f"gb-{tamper}")
    go(db, harness)
    for key in ("planner", "engineer"):
        manual_complete(db, session_factory, harness, key, tmp_path)
    agent = manual_finish_only(db, session_factory, harness, "code", tmp_path)  # completion callback not yet run
    artifact = one(rows(session_factory, Artifact, Artifact.agent_run_id == agent.id))
    if tamper == "delete_file":
        Path(artifact.storage_ref).unlink()
    elif tamper == "alter_bytes":
        Path(artifact.storage_ref).write_text("ALTERED", encoding="utf-8")
    else:
        row = db.get(Artifact, artifact.id)
        row.content_hash = "0" * 64
        db.commit()
    before = counts(session_factory)

    WorkflowExecutionService(db).on_agent_run_complete(agent.id)

    assert counts(session_factory) == before  # nothing was built for the corrupted subject
    assert harness.provider.roles() == []
    state = state_of(session_factory, harness)
    assert state["nodes"]["eval_code"] == WorkflowNodeRunStatus.FAILED
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and state["approvals"] == []


@pytest.mark.parametrize("victim", ["eval_security", "code"])
@pytest.mark.parametrize("tamper", ["delete_file", "alter_recorded_hash"])
def test_evidence_corrupted_after_the_gate_opened_can_never_be_approved(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir, tmp_path, victim, tamper
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, f"gc-{victim}-{tamper}")
    state = manual_to_gate(db, session_factory, harness, tmp_path)
    approval = one(state["approvals"])
    artifact_id = state["node_runs"][victim].output_snapshot_ref
    path = artifact_path(session_factory, artifact_id)
    original = path.read_bytes()
    if tamper == "delete_file":
        path.unlink()
    else:
        row = db.get(Artifact, artifact_id)
        recorded = row.content_hash
        row.content_hash = "f" * 64
        db.commit()
    before = counts(session_factory)

    evidence = get_evidence(client, auth_headers, approval.id).json()
    assert evidence["fingerprint_matches"] is False
    assert {e["node_key"]: e["content_intact"] for e in evidence["evidence"]}[victim] is False
    for _ in range(3):  # reconciliation neither repairs nor decides
        sweep(session_factory)
    refused = resolve_via_api(client, auth_headers, approval)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "fingerprint_mismatch"
    after = state_of(session_factory, harness)
    assert one(after["approvals"]).status == ApprovalStatus.PENDING and after["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert counts(session_factory) == before

    if tamper == "delete_file":  # restoring the exact bytes is the only way back, and only then does it decide
        path.write_bytes(original)
    else:
        row = db.get(Artifact, artifact_id)
        row.content_hash = recorded
        db.commit()
    assert resolve_via_api(client, auth_headers, approval).status_code == 200


def test_an_oversized_reviewer_output_fails_its_evaluation_before_inference_without_leaking_content(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    monkeypatch.setattr(settings, "evaluation_context_max_chars", 4000)
    harness = build_parallel(db, bootstrap.project, monkeypatch, "gd", texts={"code": "SECRETCONTENT " * 500})
    go(db, harness)
    drain(new_worker(session_factory))

    state = assert_fail_closed(session_factory, harness)
    failed = evaluation_by_node(session_factory, harness)["eval_code"]
    assert failed.status == EvaluationRunStatus.FAILED and "evaluation_context_too_large" in failed.failure_reason
    assert "SECRETCONTENT" not in failed.failure_reason
    assert "eval_code" not in harness.provider.roles()  # refused BEFORE inference: no spend for that evaluation
    assert state["nodes"]["eval_code"] == WorkflowNodeRunStatus.FAILED


@pytest.mark.parametrize("what", ["definition_deprecated", "evaluator_agent_deprecated", "definition_retired"])
def test_a_bound_evaluation_dependency_that_is_no_longer_active_refuses_the_run_at_start(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, what
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, f"ge-{what}")
    setup = harness.setups["eval_test"]
    if what == "evaluator_agent_deprecated":
        setup.agent_version.status = VersionStatus.DEPRECATED
    else:
        setup.version.status = VersionStatus.DEPRECATED if what == "definition_deprecated" else VersionStatus.RETIRED
    db.commit()
    before = counts(session_factory)

    with pytest.raises(WorkflowExecutionError, match="no longer valid; refusing to start"):
        WorkflowExecutionService(db).start_workflow_run(harness.built.published.id, harness.built.task_run.id)

    assert counts(session_factory) == before and rows(session_factory, WorkflowRun) == []
    assert harness.provider.roles() == []


def test_a_bound_dependency_deprecated_mid_run_fails_that_evaluation_closed_and_never_substitutes(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "gf")
    go(db, harness)
    solo = new_worker(session_factory)
    assert solo.run_once() is True and solo.run_once() is True  # planner, engineer
    harness.setups["eval_security"].version.status = VersionStatus.DEPRECATED  # after the start-time preflight passed
    db.commit()
    drain(solo)

    state = assert_fail_closed(session_factory, harness)
    assert state["nodes"]["eval_security"] == WorkflowNodeRunStatus.FAILED
    assert "eval_security" not in harness.provider.roles()  # no evaluator ran under a substituted version
    assert "eval_security" not in evaluation_by_node(session_factory, harness)  # and no EvaluationRun was built for it

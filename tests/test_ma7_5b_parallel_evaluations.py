"""MA7.5B -- parallel Evaluations feeding a six-parent Human Approval.

    Planner -> Engineer -> { Test, Security, Code }
    each reviewer -> its OWN single-parent EVALUATION node (MA7.5A)
    each reviewer AND each evaluation -> Human Approval (six parents) -> TERMINAL

No new engine concepts: this file proves the pieces already built -- MA7.4b
fan-out/fan-in, MA7.4c multi-parent gate and Worker lanes, MA7.5A EVALUATION
node with its MA6 builder/CAS finalizer/integrity checks -- compose correctly:
no JOIN node, no multi-artifact or aggregate Evaluation, no score, no decision.

A. Product path (real Worker(concurrency=3), engine, queue, MA6, MA3, approvals;
   only the provider is faked): approve and reject.
B. Parallel scheduling + exact counts, no cross-consumption of evidence.
C. Six-parent gate: barrier, ordering, fingerprint, evidence integrity.
D. Findings are evidence: NOT_MET never decides anything.
E. Fail-fast: reviewer failure, evaluation failure/malformed output.
F. Cancellation at five points.
G. Recovery/reconciliation.
H. Races.
I. Read model.

Every database, artifact and log is a disposable/scratch location.
"""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.db.enums import (
    AgentRunRole,
    AgentRunStatus,
    ApprovalStatus,
    EvaluationFinding,
    EvaluationRunStatus,
    JobQueueStatus,
    JobType,
    WorkflowNodeRunStatus,
    WorkflowRunStatus,
)
from app.errors import InvalidStateTransitionError
from app.models.artifacts_eval import Artifact
from app.models.evaluation_runs import EvaluationRun
from app.models.execution import JobQueue
from app.models.governance import Approval
from app.models.tasks import AgentRun, AgentRunAttempt
from app.providers.base import ProviderInvalidResponseError
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.approval_service import (
    WORKFLOW_HUMAN_APPROVAL_OPERATION,
    ApprovalService,
    compute_action_fingerprint,
)
from app.services.workflow_execution_service import WorkflowExecutionService
from app.worker import Worker
from tests.ma7_3b_support import AGENT, APPROVAL, TERMINAL, resolve_via_api, snapshot, start
from tests.ma7_4a_support import finish_agent
from tests.ma7_4b_support import (
    EVALUATION,
    RoleProvider,
    build_graph,
    evaluator_json,
    install_provider,
    make_evaluation_setup,
    new_worker,
    running_worker,
    wait_until,
)
from tests.test_ma7_5a_evaluation_node import (
    CRITERIA,
    count_events,
    counts,
    in_threads,
    results_of,
    rows,
)

REVIEWERS = ("code", "security", "test")  # job order: fan-out dispatches in node_key order
EVALS = {"code": "eval_code", "security": "eval_security", "test": "eval_test"}
GATE_PARENTS = sorted(list(REVIEWERS) + list(EVALS.values()))  # canonical node_key order
# planner, engineer, 3 reviewers, 3 evaluators
FINAL_COUNTS = {
    "tasks": 4,  # the workflow's Task + one dedicated evaluator Task per Evaluation
    "task_runs": 9,  # workflow parent + 5 agent nodes + 3 evaluators
    "agent_runs": 8,
    "evaluation_runs": 3,
    "jobs": 8,
    "criterion_results": 6,  # 3 evaluations x 2 criteria
    "approvals": 1,
}


@pytest.fixture()
def artifacts_dir(monkeypatch, tmp_path):
    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


# -- builders ----------------------------------------------------------------------------


def build_parallel(db, project, monkeypatch, label, *, findings=None, texts=None, downstream="terminal"):
    """The MA7.5B graph with real inference plumbing and a fake provider.
    ``findings``/``texts`` are keyed by provider ROLE (the node key)."""
    setups = {
        node: make_evaluation_setup(db, project, f"{label}-{node}", CRITERIA, role=node) for node in EVALS.values()
    }
    nodes = [
        ("planner", AGENT),
        ("engineer", AGENT),
        ("test", AGENT),
        ("security", AGENT),
        ("code", AGENT),
        ("eval_test", EVALUATION),
        ("eval_security", EVALUATION),
        ("eval_code", EVALUATION),
        ("gate", APPROVAL),
    ]
    nodes += [("result", TERMINAL)] if downstream == "terminal" else [("post", AGENT), ("result", TERMINAL)]
    edges = [("planner", "engineer")]
    for reviewer in REVIEWERS:
        edges += [
            ("engineer", reviewer),
            (reviewer, EVALS[reviewer]),  # each reviewer feeds its OWN evaluation ...
            (reviewer, "gate"),  # ... and the gate directly
            (EVALS[reviewer], "gate"),
        ]
    edges += [("gate", "result" if downstream == "terminal" else "post")]
    if downstream != "terminal":
        edges += [("post", "result")]
    graph = build_graph(db, project, nodes, edges, label=label, real=True, evaluations=setups)

    role_of = dict(graph.role_of)
    for setup in setups.values():
        role_of.update(setup.role_of)

    def text_for(role):
        if texts and role in texts:
            return texts[role]
        if role in setups:
            return evaluator_json(CRITERIA, (findings or {}).get(role))
        return f"OUTPUT-OF-{role}"

    provider = install_provider(monkeypatch, RoleProvider(role_of, text=text_for))
    return SimpleNamespace(graph=graph, built=graph.built, setups=setups, provider=provider, run=None)


def go(db, harness):
    harness.run = start(db, harness.built)
    return harness.run


def state_of(session_factory, harness):
    return snapshot(session_factory, harness.run.id)


def solo_until_reviewers_done(db, session_factory, harness):
    """Single worker, deterministic: Planner, Engineer, then Code, Security, Test.
    Afterwards the three Evaluations are dispatched and QUEUED (never run)."""
    go(db, harness)
    solo = new_worker(session_factory)
    for _ in range(5):
        assert solo.run_once() is True
    return solo


def drain(worker, limit=40):
    processed = 0
    while worker.run_once():
        processed += 1
        assert processed <= limit
    return processed


def evaluation_by_node(session_factory, harness):
    """{eval node key: EvaluationRun}, found through the node's agent_run_id."""
    state = state_of(session_factory, harness)
    by_agent = {e.evaluator_agent_run_id: e for e in rows(session_factory, EvaluationRun)}
    return {
        key: by_agent[state["node_runs"][key].agent_run_id]
        for key in EVALS.values()
        if state["node_runs"][key].agent_run_id in by_agent
    }


def output_artifact(session_factory, harness, key):
    return state_of(session_factory, harness)["node_runs"][key].output_snapshot_ref


def artifact_path(session_factory, artifact_id):
    session = session_factory()
    try:
        return Path(session.get(Artifact, artifact_id).storage_ref)
    finally:
        session.close()


def expected_fingerprint(session_factory, harness):
    """An INDEPENDENT recomputation of the six-entry v2 payload from raw rows."""
    state = state_of(session_factory, harness)
    session = session_factory()
    try:
        upstream = [
            {
                "node_key": key,
                "node_run_id": state["node_runs"][key].id,
                "artifact_id": state["node_runs"][key].output_snapshot_ref,
                "artifact_content_hash": session.get(Artifact, state["node_runs"][key].output_snapshot_ref).content_hash,
            }
            for key in GATE_PARENTS
        ]
    finally:
        session.close()
    payload = {
        "kind": WORKFLOW_HUMAN_APPROVAL_OPERATION,
        "workflow_run_id": harness.run.id,
        "workflow_node_run_id": state["node_runs"]["gate"].id,
        "workflow_node_id": harness.built.nodes["gate"].id,
        "node_key": "gate",
        "iteration": 0,
        "approval_group": "eng-leads",
        "upstream": upstream,
        "evidence_version": 2,
    }
    return compute_action_fingerprint(payload), upstream


def get_evidence(client, headers, approval_id):
    return client.get(f"/approvals/{approval_id}/evidence", headers=headers)


def find_node_agent(db, session_factory, harness, key):
    agent = db.get(AgentRun, state_of(session_factory, harness)["node_runs"][key].agent_run_id)
    db.refresh(agent)
    return agent


def manual_complete(db, session_factory, harness, key, tmp_path, status=AgentRunStatus.COMPLETED, *, findings=None):
    """The node's agent reaches a terminal status (with a real hashed artifact) and the
    worker's completion callback runs -- no worker, no provider: fast + deterministic."""
    agent = find_node_agent(db, session_factory, harness, key)
    body = evaluator_json(CRITERIA, findings) if key in EVALS.values() else f"OUT-{key}"
    finish_agent(db, agent, status, text=body, tmp_path=tmp_path)
    WorkflowExecutionService(db).on_agent_run_complete(agent.id)
    return agent


def manual_finish_only(db, session_factory, harness, key, tmp_path, status=AgentRunStatus.COMPLETED):
    agent = find_node_agent(db, session_factory, harness, key)
    body = evaluator_json(CRITERIA) if key in EVALS.values() else f"OUT-{key}"
    finish_agent(db, agent, status, text=body, tmp_path=tmp_path)
    return agent


def manual_to_evaluations_running(db, session_factory, harness, tmp_path):
    go(db, harness)
    for key in ("planner", "engineer", *REVIEWERS):
        manual_complete(db, session_factory, harness, key, tmp_path)
    state = state_of(session_factory, harness)
    assert {state["nodes"][e] for e in EVALS.values()} == {WorkflowNodeRunStatus.RUNNING}
    return state


def manual_to_gate(db, session_factory, harness, tmp_path):
    manual_to_evaluations_running(db, session_factory, harness, tmp_path)
    for key in EVALS.values():
        manual_complete(db, session_factory, harness, key, tmp_path)
    state = state_of(session_factory, harness)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    return state


def sweep(session_factory):
    return new_worker(session_factory).reconcile_workflows()


def decide_then_die(session_factory, bootstrap, approval, monkeypatch, *, approve=True):
    real = WorkflowExecutionService.after_approval_resolved
    monkeypatch.setattr(WorkflowExecutionService, "after_approval_resolved", lambda self, *a, **k: None)
    session = session_factory()
    try:
        ApprovalService(session).resolve(
            approval.id, user=bootstrap.user, approve=approve, action_fingerprint=approval.action_fingerprint
        )
    finally:
        session.close()
    monkeypatch.setattr(WorkflowExecutionService, "after_approval_resolved", real)


def delta(before, after):
    return {k: after[k] - before[k] for k in before}


# =============================================================================
# A. PRODUCT PATH -- real Worker(concurrency=3)
# =============================================================================


def product_hooks(harness):
    """Reviewers must all be in the provider together; then the three evaluators
    must all be in the provider together (a barrier only opens if they are --
    'queued at the same time' is not enough). The Code evaluator is then held
    so the run can be inspected mid-flight."""
    reviewers_meet = threading.Barrier(3, timeout=25)
    evaluators_meet = threading.Barrier(3, timeout=25)
    release_code_eval = threading.Event()
    errors = []

    def meeting(barrier):
        def hook(_request):
            try:
                barrier.wait()
            except Exception as exc:  # only on a regression: they were NOT concurrent
                errors.append(exc)
                raise

        return hook

    for reviewer in REVIEWERS:
        harness.provider.hooks[reviewer] = meeting(reviewers_meet)
    for evaluation in EVALS.values():
        harness.provider.hooks[evaluation] = meeting(evaluators_meet)

    def held(_request):
        meeting(evaluators_meet)(_request)
        assert release_code_eval.wait(30)

    harness.provider.hooks["eval_code"] = held
    return release_code_eval, errors


def test_product_path_parallel_evaluations_and_six_parent_approval_approve(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(
        db,
        bootstrap.project,
        monkeypatch,
        "pa",
        findings={"eval_security": {"security": "NOT_MET"}, "eval_code": {"correctness": "PARTIAL"}},
    )
    release_code_eval, errors = product_hooks(harness)
    go(db, harness)
    parent = harness.built.task_run.id

    with running_worker(session_factory, 3) as the_worker:
        # -- the two evaluators that are not held finish first ...
        assert wait_until(
            lambda: {
                k: v
                for k, v in state_of(session_factory, harness)["nodes"].items()
                if k in ("eval_test", "eval_security")
            }
            == {"eval_test": WorkflowNodeRunStatus.COMPLETED, "eval_security": WorkflowNodeRunStatus.COMPLETED}
        )
        mid = state_of(session_factory, harness)
        # ... and, with all three evaluators having been in the provider simultaneously
        # (the barrier opened), the gate is NOT here: six parents, one still running
        assert mid["nodes"]["eval_code"] == WorkflowNodeRunStatus.RUNNING
        assert {mid["nodes"][r] for r in REVIEWERS} == {WorkflowNodeRunStatus.COMPLETED}
        assert mid["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and mid["approvals"] == []
        assert mid["node_runs"]["gate"].agent_run_id is None
        assert mid["run"] == WorkflowRunStatus.RUNNING
        assert len(rows(session_factory, EvaluationRun)) == 3  # all three exist; one still RUNNING

        release_code_eval.set()
        assert wait_until(
            lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
        )
        assert wait_until(lambda: {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE})

        state = state_of(session_factory, harness)
        assert errors == []  # nobody waited on a broken barrier

        # -- every agent node executed exactly once; nothing extra
        assert sorted(harness.provider.roles()) == sorted(
            ["planner", "engineer", *REVIEWERS, *EVALS.values()]
        )
        assert harness.provider.roles()[:2] == ["planner", "engineer"]
        assert counts(session_factory) == FINAL_COUNTS

        # -- three lanes of the ONE worker ran the three evaluators concurrently
        evaluator_agents = [state["node_runs"][e].agent_run_id for e in EVALS.values()]
        lanes = {
            a.worker_id
            for a in rows(session_factory, AgentRunAttempt, AgentRunAttempt.agent_run_id.in_(evaluator_agents))
        }
        assert len(lanes) == 3 and all(lane.startswith(the_worker.worker_id + ":lane") for lane in lanes)

        # -- exactly three EvaluationRuns; each binds ITS reviewer's exact AgentRun / artifact / hash
        evaluations = evaluation_by_node(session_factory, harness)
        assert len(evaluations) == 3
        for reviewer, node in EVALS.items():
            evaluation = evaluations[node]
            reviewer_agent = state["node_runs"][reviewer].agent_run_id
            artifacts = rows(session_factory, Artifact, Artifact.agent_run_id == reviewer_agent)
            assert len(artifacts) == 1  # the reviewer output is preserved, once
            assert evaluation.subject_agent_run_id == reviewer_agent
            assert evaluation.subject_artifact_id == artifacts[0].id == state["node_runs"][reviewer].output_snapshot_ref
            assert evaluation.subject_artifact_content_hash == artifacts[0].content_hash
            assert evaluation.status == EvaluationRunStatus.COMPLETED
            assert evaluation.evaluator_agent_run_id == state["node_runs"][node].agent_run_id
            assert evaluation.evaluator_agent_version_id == harness.setups[node].agent_version.id

        # -- findings persist independently (and one NOT_MET decided nothing)
        assert results_of(session_factory, evaluations["eval_test"].id) == {
            "correctness": EvaluationFinding.MET,
            "security": EvaluationFinding.MET,
        }
        assert results_of(session_factory, evaluations["eval_security"].id) == {
            "correctness": EvaluationFinding.MET,
            "security": EvaluationFinding.NOT_MET,
        }
        assert results_of(session_factory, evaluations["eval_code"].id) == {
            "correctness": EvaluationFinding.PARTIAL,
            "security": EvaluationFinding.MET,
        }

        # -- exactly ONE Approval, PENDING, waiting; six-entry evidence, canonical order, intact
        (approval,) = state["approvals"]
        assert approval.status == ApprovalStatus.PENDING and approval.resolved_by is None
        fingerprint, upstream = expected_fingerprint(session_factory, harness)
        assert approval.action_fingerprint == fingerprint
        body = get_evidence(client, auth_headers, approval.id).json()
        assert body["evidence_version"] == 2 and body["fingerprint_matches"] is True
        assert [e["node_key"] for e in body["evidence"]] == GATE_PARENTS == [u["node_key"] for u in upstream]
        assert len(body["evidence"]) == 6
        for entry, bound in zip(body["evidence"], upstream):
            assert entry["node_run_id"] == bound["node_run_id"] and entry["artifact_id"] == bound["artifact_id"]
            assert entry["content_hash"] == bound["artifact_content_hash"] and entry["content_intact"] is True
        # both the original reviewer outputs AND the evaluator outputs about them
        reviewer_ids = {state["node_runs"][r].output_snapshot_ref for r in REVIEWERS}
        evaluator_ids = {state["node_runs"][e].output_snapshot_ref for e in EVALS.values()}
        assert {e["artifact_id"] for e in body["evidence"]} == reviewer_ids | evaluator_ids and len(reviewer_ids | evaluator_ids) == 6
        assert count_events(session_factory, parent, "approval.requested") == 1

        # -- no cross-consumption: each evaluator saw its OWN reviewer's output and nobody else's
        for reviewer, node in EVALS.items():
            prompt = harness.provider.prompt_of(node)
            assert f"OUTPUT-OF-{reviewer}" in prompt
            for other in (r for r in REVIEWERS if r != reviewer):
                assert f"OUTPUT-OF-{other}" not in prompt

        # -- a NOT_MET / PARTIAL does not decide; time, polls and sweeps do not either
        for _ in range(3):
            sweep(session_factory)
        assert state_of(session_factory, harness)["approvals"][0].status == ApprovalStatus.PENDING
        assert state_of(session_factory, harness)["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL

        # -- the human approves; the workflow completes
        assert resolve_via_api(client, auth_headers, approval).status_code == 200
        final = state_of(session_factory, harness)
        assert final["run"] == WorkflowRunStatus.COMPLETED and final["run_ended_at"] is not None
        assert set(final["nodes"].values()) == {WorkflowNodeRunStatus.COMPLETED}
        assert counts(session_factory) == FINAL_COUNTS  # the decision created nothing

    # -- restart / reconciliation: nothing new, ever
    before, types_before = counts(session_factory), [e for e in rows(session_factory, Approval)]
    for _ in range(3):
        sweep(session_factory)
    in_threads(4, lambda _i: sweep(session_factory))
    assert counts(session_factory) == before == FINAL_COUNTS
    assert len(types_before) == 1 and count_events(session_factory, parent, "workflow.completed") == 1


def test_product_path_human_reject_in_an_independent_run(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(
        db,
        bootstrap.project,
        monkeypatch,
        "pr",
        findings={role: {"correctness": "NOT_MET", "security": "NOT_MET"} for role in EVALS.values()},
    )
    release_code_eval, errors = product_hooks(harness)
    go(db, harness)
    with running_worker(session_factory, 3):
        release_code_eval.set()
        assert wait_until(
            lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
        )
        assert wait_until(lambda: {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE})
        waiting = state_of(session_factory, harness)
        approval = waiting["approvals"][0]
        outputs = {k: waiting["node_runs"][k].output_snapshot_ref for k in GATE_PARENTS}

        assert resolve_via_api(client, auth_headers, approval, approve=False).status_code == 200
        assert wait_until(lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.FAILED)

    assert errors == []
    final = state_of(session_factory, harness)
    assert final["run_ended_at"] is not None
    assert final["nodes"]["gate"] == WorkflowNodeRunStatus.FAILED
    assert final["nodes"]["result"] == WorkflowNodeRunStatus.PENDING  # nothing downstream ran
    assert final["approvals"][0].status == ApprovalStatus.REJECTED
    for key in GATE_PARENTS:  # all six pieces of evidence preserved
        assert final["nodes"][key] == WorkflowNodeRunStatus.COMPLETED
        assert final["node_runs"][key].output_snapshot_ref == outputs[key]
    assert {e.status for e in rows(session_factory, EvaluationRun)} == {EvaluationRunStatus.COMPLETED}
    assert counts(session_factory) == FINAL_COUNTS
    assert len(harness.provider.roles()) == 8
    assert resolve_via_api(client, auth_headers, approval, approve=False).status_code == 200  # idempotent replay
    assert resolve_via_api(client, auth_headers, approval, approve=True).status_code == 409  # opposite conflicts


# =============================================================================
# B. Parallel scheduling, exact counts
# =============================================================================


def test_each_evaluation_becomes_ready_independently_of_its_siblings(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "b1")
    go(db, harness)
    for key in ("planner", "engineer"):
        manual_complete(db, session_factory, harness, key, tmp_path)
    assert {state_of(session_factory, harness)["nodes"][e] for e in EVALS.values()} == {WorkflowNodeRunStatus.PENDING}

    manual_complete(db, session_factory, harness, "security", tmp_path)  # ONLY security finished

    state = state_of(session_factory, harness)
    assert state["nodes"]["eval_security"] == WorkflowNodeRunStatus.RUNNING  # dispatched without waiting for the others
    assert state["nodes"]["eval_test"] == state["nodes"]["eval_code"] == WorkflowNodeRunStatus.PENDING
    assert len(rows(session_factory, EvaluationRun)) == 1
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING


def test_a_single_evaluation_creates_exactly_one_of_each_row(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "b2")
    go(db, harness)
    for key in ("planner", "engineer"):
        manual_complete(db, session_factory, harness, key, tmp_path)
    before = counts(session_factory)

    manual_complete(db, session_factory, harness, "code", tmp_path)

    # one reviewer completion => exactly one evaluator Task/TaskRun/AgentRun/EvaluationRun/job
    assert delta(before, counts(session_factory)) == {
        "tasks": 1,
        "task_runs": 1,
        "agent_runs": 1,
        "evaluation_runs": 1,
        "jobs": 1,
        "criterion_results": 0,
        "approvals": 0,
    }
    evaluation = evaluation_by_node(session_factory, harness)["eval_code"]
    assert evaluation.subject_agent_run_id == find_node_agent(db, session_factory, harness, "code").id
    assert evaluation.evaluator_agent_run_id != evaluation.subject_agent_run_id


def test_three_reviewers_yield_exactly_three_of_everything_and_no_evaluation_shares_a_row(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "b3")
    manual_to_evaluations_running(db, session_factory, harness, tmp_path)

    evaluations = evaluation_by_node(session_factory, harness)
    assert set(evaluations) == set(EVALS.values())
    for column in (
        "subject_agent_run_id",
        "subject_artifact_id",
        "evaluator_agent_run_id",
        "evaluator_task_run_id",
        "id",
    ):
        assert len({getattr(e, column) for e in evaluations.values()}) == 3  # three DISTINCT of each
    now = counts(session_factory)
    # 3 evaluator Tasks; jobs = Planner, Engineer, 3 reviewers + 3 evaluators (manual mode enqueues, never consumes)
    assert (now["tasks"], now["evaluation_runs"], now["jobs"]) == (4, 3, 8)
    state = state_of(session_factory, harness)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and state["approvals"] == []
    jobs = rows(session_factory, JobQueue, JobQueue.job_type == JobType.AGENT_RUN)
    evaluator_agents = {state["node_runs"][e].agent_run_id for e in EVALS.values()}
    assert sum(1 for j in jobs if j.payload_ref in evaluator_agents) == 3  # exactly one AGENT_RUN job each


def test_three_evaluation_nodes_never_consume_a_sibling_reviewers_artifact(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "b4")
    manual_to_evaluations_running(db, session_factory, harness, tmp_path)
    for reviewer, node in EVALS.items():
        evaluation = evaluation_by_node(session_factory, harness)[node]
        own = state_of(session_factory, harness)["node_runs"][reviewer]
        assert evaluation.subject_artifact_id == own.output_snapshot_ref
        agent = db.get(AgentRun, evaluation.evaluator_agent_run_id)
        db.refresh(agent)
        context = agent.input_context_json
        assert context["kind"] == "evaluation_request"
        assert context["subject_artifact_id"] == own.output_snapshot_ref  # one subject, its own
        assert context["upstream_node_run_ids"] == [own.id]


def test_the_scheduler_dispatches_evaluations_without_any_join_or_bundle_row(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    """No new node type, no synthetic/bundle/aggregate artifact: only agent + evaluator artifacts exist."""
    harness = build_parallel(db, bootstrap.project, monkeypatch, "b5")
    manual_to_gate(db, session_factory, harness, tmp_path)
    types = {n.node_type.value for n in harness.built.published.nodes}
    assert types == {"agent", "evaluation", "human_approval", "terminal"}
    artifacts = rows(session_factory, Artifact)
    owners = {a.agent_run_id for a in artifacts}
    state = state_of(session_factory, harness)
    expected_owners = {
        state["node_runs"][k].agent_run_id for k in ("planner", "engineer", *REVIEWERS, *EVALS.values())
    }
    assert owners == expected_owners and len(artifacts) == 8  # one artifact per agent/evaluator run, nothing combined


# =============================================================================
# C. The six-parent barrier, ordering and evidence integrity
# =============================================================================


def test_the_gate_waits_for_all_six_parents(db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "c1")
    manual_to_evaluations_running(db, session_factory, harness, tmp_path)
    # Reviewers are ALL done (three of the six parents); each evaluation completes in turn
    for done, key in enumerate(("eval_security", "eval_code", "eval_test"), start=1):
        assert state_of(session_factory, harness)["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING
        assert state_of(session_factory, harness)["approvals"] == []
        manual_complete(db, session_factory, harness, key, tmp_path)
        assert done <= 3
    state = state_of(session_factory, harness)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL and len(state["approvals"]) == 1


def test_the_gate_does_not_open_if_only_the_reviewers_completed(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "c2")
    state = manual_to_evaluations_running(db, session_factory, harness, tmp_path)
    for _ in range(3):
        sweep(session_factory)
    assert state_of(session_factory, harness)["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING  # three of six parents
    assert state["approvals"] == [] and counts(session_factory)["approvals"] == 0


def test_the_fingerprint_binds_all_six_outputs_in_node_key_order(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "c3")
    state = manual_to_gate(db, session_factory, harness, tmp_path)
    approval = state["approvals"][0]
    fingerprint, upstream = expected_fingerprint(session_factory, harness)
    assert approval.action_fingerprint == fingerprint
    assert [u["node_key"] for u in upstream] == ["code", "eval_code", "eval_security", "eval_test", "security", "test"]
    assert len({u["artifact_id"] for u in upstream}) == 6 and all(u["artifact_content_hash"] for u in upstream)
    assert approval.bound_artifact_id == upstream[0]["artifact_id"]
    body = get_evidence(client, auth_headers, approval.id).json()
    assert body["fingerprint_matches"] is True and [e["node_key"] for e in body["evidence"]] == GATE_PARENTS
    assert all(e["content_intact"] for e in body["evidence"])


@pytest.mark.parametrize("victim", GATE_PARENTS)
def test_tampering_with_any_of_the_six_bound_artifacts_fails_approval_closed(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir, tmp_path, victim
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, f"c4-{victim}")
    state = manual_to_gate(db, session_factory, harness, tmp_path)
    approval = state["approvals"][0]
    path = artifact_path(session_factory, state["node_runs"][victim].output_snapshot_ref)
    original = path.read_bytes()
    before = counts(session_factory)

    path.write_text("altered after the gate was created", encoding="utf-8")

    evidence = get_evidence(client, auth_headers, approval.id).json()
    assert evidence["fingerprint_matches"] is False
    assert {e["node_key"]: e["content_intact"] for e in evidence["evidence"]} == {
        key: key != victim for key in GATE_PARENTS
    }
    response = resolve_via_api(client, auth_headers, approval)
    assert response.status_code == 409 and response.json()["error"]["code"] == "fingerprint_mismatch"
    after = state_of(session_factory, harness)
    assert after["approvals"][0].status == ApprovalStatus.PENDING and after["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert counts(session_factory) == before  # nothing advanced, nothing created

    path.write_bytes(original)  # restore -> the SAME approval decides normally
    assert get_evidence(client, auth_headers, approval.id).json()["fingerprint_matches"] is True
    assert resolve_via_api(client, auth_headers, approval).status_code == 200
    assert state_of(session_factory, harness)["run"] == WorkflowRunStatus.COMPLETED


def test_the_ma75a_subject_integrity_checks_still_stop_a_tampered_reviewer_before_its_evaluation(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "c5")
    go(db, harness)
    for key in ("planner", "engineer"):
        manual_complete(db, session_factory, harness, key, tmp_path)
    agent = manual_finish_only(db, session_factory, harness, "security", tmp_path)  # callback not yet run
    artifact = rows(session_factory, Artifact, Artifact.agent_run_id == agent.id)[0]
    Path(artifact.storage_ref).write_text("ALTERED", encoding="utf-8")
    before = counts(session_factory)

    WorkflowExecutionService(db).on_agent_run_complete(agent.id)

    state = state_of(session_factory, harness)
    assert state["nodes"]["eval_security"] == WorkflowNodeRunStatus.FAILED  # refused at dispatch
    assert state["run"] in (WorkflowRunStatus.RUNNING, WorkflowRunStatus.FAILED)  # fail-fast under way
    assert counts(session_factory) == before  # no evaluator rows built for the tampered subject
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and state["approvals"] == []


# =============================================================================
# D. Findings are evidence -- they never decide
# =============================================================================


@pytest.mark.parametrize(
    "findings",
    [
        {role: {"correctness": "NOT_MET", "security": "NOT_MET"} for role in EVALS.values()},
        {"eval_code": {"correctness": "NOT_MET"}},
        {role: {"correctness": "NOT_APPLICABLE", "security": "PARTIAL"} for role in EVALS.values()},
    ],
    ids=["all-not-met", "one-not-met", "not-applicable-and-partial"],
)
def test_no_combination_of_findings_approves_rejects_or_moves_the_gate(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path, findings
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "d1", findings=findings)
    go(db, harness)
    for key in ("planner", "engineer", *REVIEWERS):
        manual_complete(db, session_factory, harness, key, tmp_path)
    for node in EVALS.values():
        manual_complete(db, session_factory, harness, node, tmp_path, findings=findings.get(node))

    state = state_of(session_factory, harness)
    # every EvaluationRun COMPLETED (a NOT_MET is a valid, complete evaluation) ...
    assert {e.status for e in rows(session_factory, EvaluationRun)} == {EvaluationRunStatus.COMPLETED}
    assert {state["nodes"][e] for e in EVALS.values()} == {WorkflowNodeRunStatus.COMPLETED}
    # ... and the gate is exactly what normal completion made it: waiting, PENDING, undecided
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    (approval,) = state["approvals"]
    assert approval.status == ApprovalStatus.PENDING and approval.resolved_by is None and approval.resolved_at is None
    for _ in range(3):
        sweep(session_factory)
    again = state_of(session_factory, harness)
    assert again["approvals"][0].status == ApprovalStatus.PENDING and again["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert counts(session_factory)["approvals"] == 1
    assert state["nodes"]["result"] == WorkflowNodeRunStatus.PENDING  # undecided => downstream never ran


# =============================================================================
# E. Fail-fast
# =============================================================================


def test_a_reviewer_failure_keeps_the_ma74_fail_fast_behavior_and_never_creates_the_gate(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "e1")
    harness.provider.hooks["security"] = lambda _r: (_ for _ in ()).throw(ProviderInvalidResponseError("boom"))
    go(db, harness)
    drain(new_worker(session_factory))

    state = state_of(session_factory, harness)
    assert state["run"] == WorkflowRunStatus.FAILED and state["run_ended_at"] is not None
    assert state["nodes"]["security"] == WorkflowNodeRunStatus.FAILED
    assert state["nodes"]["code"] == WorkflowNodeRunStatus.COMPLETED  # completed sibling preserved
    assert state["nodes"]["eval_security"] == WorkflowNodeRunStatus.PENDING  # downstream of the failure never ran
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and state["approvals"] == []
    # nothing evaluates a failed reviewer; whatever was already in flight was stopped, not left dangling
    assert "eval_security" not in harness.provider.roles()
    assert {e.status for e in rows(session_factory, EvaluationRun)} <= {
        EvaluationRunStatus.COMPLETED,
        EvaluationRunStatus.CANCELLED,
    }
    assert {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE}


def test_a_malformed_evaluator_response_fails_fast_and_stops_the_queued_sibling_evaluations(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "e2", texts={"eval_code": "not json at all"})
    go(db, harness)
    drain(new_worker(session_factory))  # eval_code's job is first: it fails; its queued siblings are cancelled

    state = state_of(session_factory, harness)
    evaluations = evaluation_by_node(session_factory, harness)
    assert evaluations["eval_code"].status == EvaluationRunStatus.FAILED
    assert results_of(session_factory, evaluations["eval_code"].id) == {}
    assert state["nodes"]["eval_code"] == WorkflowNodeRunStatus.FAILED
    for sibling in ("eval_security", "eval_test"):
        assert evaluations[sibling].status == EvaluationRunStatus.CANCELLED
        assert state["nodes"][sibling] == WorkflowNodeRunStatus.CANCELLED
    assert "eval_security" not in harness.provider.roles() and "eval_test" not in harness.provider.roles()
    assert state["run"] == WorkflowRunStatus.FAILED and state["run_ended_at"] is not None
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and state["approvals"] == []
    assert {state["nodes"][r] for r in REVIEWERS} == {WorkflowNodeRunStatus.COMPLETED}  # reviewer evidence preserved
    assert count_events(session_factory, harness.built.task_run.id, "workflow.failed") == 1


def test_an_in_flight_sibling_evaluation_is_cancelled_and_a_completed_one_is_preserved(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "e3")
    code_in_flight, security_in_flight = threading.Event(), threading.Event()
    release_code, fail_security = threading.Event(), threading.Event()

    def eval_code(_request):
        code_in_flight.set()
        assert release_code.wait(30)

    def eval_security(_request):
        security_in_flight.set()
        assert fail_security.wait(30)
        raise ProviderInvalidResponseError("evaluator down")

    harness.provider.hooks.update(eval_code=eval_code, eval_security=eval_security)  # eval_test: immediate
    go(db, harness)
    with running_worker(session_factory, 3):
        assert wait_until(
            lambda: state_of(session_factory, harness)["nodes"]["eval_test"] == WorkflowNodeRunStatus.COMPLETED
            and code_in_flight.is_set()
            and security_in_flight.is_set()
        )
        fail_security.set()
        assert wait_until(lambda: state_of(session_factory, harness)["nodes"]["eval_security"] == WorkflowNodeRunStatus.FAILED)
        code_agent_id = state_of(session_factory, harness)["node_runs"]["eval_code"].agent_run_id
        assert wait_until(lambda: db.get(AgentRun, code_agent_id) and _requested(db, code_agent_id))
        mid = state_of(session_factory, harness)
        # the in-flight sibling was signalled but is mid-call: the run has NOT finalized, no gate
        assert mid["nodes"]["eval_code"] == WorkflowNodeRunStatus.RUNNING and mid["run"] == WorkflowRunStatus.RUNNING
        assert mid["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and mid["approvals"] == []

        release_code.set()
        assert wait_until(lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.FAILED)

    state = state_of(session_factory, harness)
    evaluations = evaluation_by_node(session_factory, harness)
    assert state["nodes"]["eval_test"] == WorkflowNodeRunStatus.COMPLETED  # preserved, with its findings
    assert evaluations["eval_test"].status == EvaluationRunStatus.COMPLETED
    assert len(results_of(session_factory, evaluations["eval_test"].id)) == 2
    assert state["nodes"]["eval_code"] == WorkflowNodeRunStatus.CANCELLED
    assert evaluations["eval_code"].status == EvaluationRunStatus.CANCELLED  # even though the call returned
    assert results_of(session_factory, evaluations["eval_code"].id) == {}
    assert evaluations["eval_security"].status == EvaluationRunStatus.FAILED
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and state["approvals"] == []
    assert {state["nodes"][r] for r in REVIEWERS} == {WorkflowNodeRunStatus.COMPLETED}
    assert count_events(session_factory, harness.built.task_run.id, "workflow.failed") == 1


def _requested(db, agent_run_id):
    agent = db.get(AgentRun, agent_run_id)
    db.refresh(agent)
    return agent.cancellation_requested_at is not None


# =============================================================================
# F. Cancellation at five points
# =============================================================================


def assert_nothing_dangling(session_factory):
    """Cancellation leaves no live work: every job DONE/released, every EvaluationRun and
    AgentRun terminal -- and nothing beyond what the run legitimately created."""
    assert {j.status for j in rows(session_factory, JobQueue)} <= {JobQueueStatus.DONE, JobQueueStatus.FAILED}
    assert all(e.status not in (EvaluationRunStatus.PENDING, EvaluationRunStatus.RUNNING) for e in rows(session_factory, EvaluationRun))
    assert all(
        a.status in (AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.STOPPED)
        for a in rows(session_factory, AgentRun)
    )


def test_cancel_before_the_reviewer_fan_out(db, session_factory, bootstrap, monkeypatch, artifacts_dir):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "f1")
    go(db, harness)  # Planner queued
    WorkflowExecutionService(db).cancel_workflow_run(harness.run.id, cancelled_by_user_id=bootstrap.user.id)
    drain(new_worker(session_factory))

    state = state_of(session_factory, harness)
    assert state["run"] == WorkflowRunStatus.CANCELLED and state["run_ended_at"] is not None
    assert harness.provider.roles() == []  # the queued Planner stopped at its first checkpoint
    now = counts(session_factory)
    assert (now["agent_runs"], now["evaluation_runs"], now["approvals"]) == (1, 0, 0)  # nothing fanned out
    assert_nothing_dangling(session_factory)


def test_cancel_while_the_reviewers_are_running(db, session_factory, bootstrap, monkeypatch, artifacts_dir):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "f2")
    go(db, harness)
    solo = new_worker(session_factory)
    solo.run_once()
    solo.run_once()  # Planner, Engineer -> three queued reviewers
    WorkflowExecutionService(db).cancel_workflow_run(harness.run.id, cancelled_by_user_id=bootstrap.user.id)
    drain(solo)

    state = state_of(session_factory, harness)
    assert state["run"] == WorkflowRunStatus.CANCELLED
    assert {state["nodes"][r] for r in REVIEWERS} == {WorkflowNodeRunStatus.CANCELLED}
    assert {state["nodes"][e] for e in (*EVALS.values(), "gate", "result")} == {WorkflowNodeRunStatus.CANCELLED}
    assert harness.provider.roles() == ["planner", "engineer"]
    now = counts(session_factory)
    assert (now["evaluation_runs"], now["approvals"]) == (0, 0)  # no evaluation was ever built
    assert now["agent_runs"] == 5
    assert_nothing_dangling(session_factory)


def test_cancel_after_reviewers_complete_while_the_evaluations_are_queued(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "f3")
    solo = solo_until_reviewers_done(db, session_factory, harness)
    assert {state_of(session_factory, harness)["nodes"][e] for e in EVALS.values()} == {WorkflowNodeRunStatus.RUNNING}
    WorkflowExecutionService(db).cancel_workflow_run(harness.run.id, cancelled_by_user_id=bootstrap.user.id)
    drain(solo)

    state = state_of(session_factory, harness)
    assert state["run"] == WorkflowRunStatus.CANCELLED and state["run_ended_at"] is not None
    assert {e.status for e in rows(session_factory, EvaluationRun)} == {EvaluationRunStatus.CANCELLED}
    assert {state["nodes"][e] for e in EVALS.values()} == {WorkflowNodeRunStatus.CANCELLED}
    assert {state["nodes"][r] for r in REVIEWERS} == {WorkflowNodeRunStatus.COMPLETED}  # reviewer evidence preserved
    assert not [role for role in harness.provider.roles() if role in EVALS.values()]  # no evaluator inference
    assert counts(session_factory)["criterion_results"] == 0 and state["approvals"] == []
    assert_nothing_dangling(session_factory)


def test_cancel_while_three_evaluations_are_in_flight(db, session_factory, bootstrap, monkeypatch, artifacts_dir):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "f4")
    in_flight = {node: threading.Event() for node in EVALS.values()}
    release = threading.Event()

    def hold(node):
        def hook(_request):
            in_flight[node].set()
            assert release.wait(30)

        return hook

    for node in EVALS.values():
        harness.provider.hooks[node] = hold(node)
    go(db, harness)
    with running_worker(session_factory, 3):
        assert wait_until(lambda: all(event.is_set() for event in in_flight.values()))  # all three mid-call
        WorkflowExecutionService(db).cancel_workflow_run(harness.run.id, cancelled_by_user_id=bootstrap.user.id)
        assert state_of(session_factory, harness)["run"] == WorkflowRunStatus.CANCELLING
        release.set()
        assert wait_until(lambda: state_of(session_factory, harness)["run"] == WorkflowRunStatus.CANCELLED)

    state = state_of(session_factory, harness)
    assert {e.status for e in rows(session_factory, EvaluationRun)} == {EvaluationRunStatus.CANCELLED}
    assert counts(session_factory)["criterion_results"] == 0  # a cancelled evaluation never publishes findings
    assert {state["nodes"][e] for e in EVALS.values()} == {WorkflowNodeRunStatus.CANCELLED}
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.CANCELLED and state["approvals"] == []
    assert {state["nodes"][r] for r in REVIEWERS} == {WorkflowNodeRunStatus.COMPLETED}
    assert_nothing_dangling(session_factory)


def test_cancel_after_all_six_parents_completed_and_the_gate_is_waiting(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "f5")
    state = manual_to_gate(db, session_factory, harness, tmp_path)
    approval = state["approvals"][0]
    outputs = {k: state["node_runs"][k].output_snapshot_ref for k in GATE_PARENTS}

    WorkflowExecutionService(db).cancel_workflow_run(harness.run.id, cancelled_by_user_id=bootstrap.user.id)

    final = state_of(session_factory, harness)
    assert final["approvals"][0].status == ApprovalStatus.EXPIRED
    assert final["approvals"][0].resolution_note == "workflow_cancelled"
    assert final["nodes"]["gate"] == final["nodes"]["result"] == WorkflowNodeRunStatus.CANCELLED
    assert final["run"] == WorkflowRunStatus.CANCELLED and final["run_ended_at"] is not None
    assert {k: final["node_runs"][k].output_snapshot_ref for k in GATE_PARENTS} == outputs  # all six preserved
    assert resolve_via_api(client, auth_headers, approval).status_code == 409  # cannot approve a cancelled run
    assert counts(session_factory)["approvals"] == 1


# =============================================================================
# G. Recovery / reconciliation
# =============================================================================


def test_one_evaluation_claimed_but_its_job_was_never_enqueued(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "g1")
    real_enqueue = JobQueueRepository.enqueue
    tripped = {"done": False}

    def dies_once_for_an_evaluator(self, db_, *, job_type, payload_ref):
        if job_type == JobType.AGENT_RUN and not tripped["done"]:
            agent = db_.get(AgentRun, payload_ref)
            if agent is not None and agent.role == AgentRunRole.EVALUATOR:
                tripped["done"] = True
                raise RuntimeError("died before enqueue")
        return real_enqueue(self, db_, job_type=job_type, payload_ref=payload_ref)

    monkeypatch.setattr(JobQueueRepository, "enqueue", dies_once_for_an_evaluator)
    go(db, harness)
    solo = new_worker(session_factory)
    for _ in range(5):
        solo.run_once()
    monkeypatch.setattr(JobQueueRepository, "enqueue", real_enqueue)
    assert tripped["done"]
    stranded = state_of(session_factory, harness)
    assert stranded["nodes"]["eval_code"] == WorkflowNodeRunStatus.RUNNING  # claimed; rows durable; NO job
    assert counts(session_factory)["jobs"] == 7 and counts(session_factory)["evaluation_runs"] == 3

    for _ in range(3):
        sweep(session_factory)
    in_threads(4, lambda _i: sweep(session_factory))
    assert counts(session_factory)["jobs"] == 8  # exactly one job restored, however often it ran
    drain(solo)
    assert state_of(session_factory, harness)["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert counts(session_factory) == FINAL_COUNTS


def suppress_evaluator_completion(monkeypatch, *, notify=True, callback=True):
    import app.services.execution_service as execution_service_module

    saved = (execution_service_module.notify_evaluation_run_terminal, Worker._reconcile_workflow_node)
    if notify:
        monkeypatch.setattr(execution_service_module, "notify_evaluation_run_terminal", lambda db_, task_run: None)
    if callback:
        monkeypatch.setattr(Worker, "_reconcile_workflow_node", lambda self, db_, agent_run_id: None)

    def restore():
        monkeypatch.setattr(execution_service_module, "notify_evaluation_run_terminal", saved[0])
        monkeypatch.setattr(Worker, "_reconcile_workflow_node", saved[1])

    return restore


def test_one_evaluator_terminal_but_its_evaluation_run_not_finalized(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "g2")
    solo = solo_until_reviewers_done(db, session_factory, harness)
    restore = suppress_evaluator_completion(monkeypatch)  # the process dies right after the TaskRun's terminal commit
    assert solo.run_once() is True  # eval_code's job
    restore()
    unfinalized = evaluation_by_node(session_factory, harness)["eval_code"]
    assert unfinalized.status == EvaluationRunStatus.RUNNING
    assert state_of(session_factory, harness)["nodes"]["eval_code"] == WorkflowNodeRunStatus.RUNNING
    drain(solo)  # the other two evaluations complete normally; the gate must still wait for eval_code
    assert state_of(session_factory, harness)["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING
    before = counts(session_factory)

    for _ in range(3):
        sweep(session_factory)
    in_threads(4, lambda _i: sweep(session_factory))

    state = state_of(session_factory, harness)
    healed = evaluation_by_node(session_factory, harness)["eval_code"]
    assert healed.status == EvaluationRunStatus.COMPLETED and len(results_of(session_factory, healed.id)) == 2
    assert state["nodes"]["eval_code"] == WorkflowNodeRunStatus.COMPLETED
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL and len(state["approvals"]) == 1
    assert delta(before, counts(session_factory)) == {
        "tasks": 0, "task_runs": 0, "agent_runs": 0, "evaluation_runs": 0, "jobs": 0, "criterion_results": 2, "approvals": 1,
    }
    assert counts(session_factory) == FINAL_COUNTS


def test_one_evaluation_run_finalized_but_its_node_was_not_mapped(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "g3")
    solo = solo_until_reviewers_done(db, session_factory, harness)
    restore = suppress_evaluator_completion(monkeypatch, notify=False)  # MA6 finalizes; the worker dies before the node callback
    assert solo.run_once() is True
    restore()
    assert evaluation_by_node(session_factory, harness)["eval_code"].status == EvaluationRunStatus.COMPLETED
    assert state_of(session_factory, harness)["nodes"]["eval_code"] == WorkflowNodeRunStatus.RUNNING
    drain(solo)

    for _ in range(2):
        sweep(session_factory)
    in_threads(4, lambda _i: sweep(session_factory))

    state = state_of(session_factory, harness)
    assert state["nodes"]["eval_code"] == WorkflowNodeRunStatus.COMPLETED
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL and len(state["approvals"]) == 1
    assert count_events(session_factory, harness.built.task_run.id, "workflow.node.completed", state["node_runs"]["eval_code"].id) == 1
    assert counts(session_factory) == FINAL_COUNTS


def test_two_evaluations_complete_while_the_third_worker_died_mid_job(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "g4")
    solo = solo_until_reviewers_done(db, session_factory, harness)
    assert solo.run_once() is True and solo.run_once() is True  # eval_code, eval_security
    claim_session = session_factory()
    try:  # a worker claims the third evaluator's job, then dies: its lease expires unfinished
        claimed = JobQueueRepository().claim_one(claim_session, worker_id="dead-worker", lease_seconds=30)
        assert claimed is not None
        from datetime import datetime, timedelta, timezone

        claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        claim_session.commit()
        job_id = claimed.id
    finally:
        claim_session.close()
    assert state_of(session_factory, harness)["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING

    assert solo.run_once() is True  # another worker reclaims the abandoned job (new fencing token) and finishes it

    state = state_of(session_factory, harness)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL and len(state["approvals"]) == 1
    assert {e.status for e in rows(session_factory, EvaluationRun)} == {EvaluationRunStatus.COMPLETED}
    reclaimed = rows(session_factory, JobQueue, JobQueue.id == job_id)[0]
    assert reclaimed.attempt_count == 2 and reclaimed.status == JobQueueStatus.DONE
    assert counts(session_factory) == FINAL_COUNTS  # one execution of the abandoned evaluation, not two


def test_all_three_evaluations_complete_but_the_gate_was_never_created(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "g5")
    real = WorkflowExecutionService._dispatch_human_approval
    monkeypatch.setattr(
        WorkflowExecutionService,
        "_dispatch_human_approval",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("process died")),
    )
    go(db, harness)
    drain(new_worker(session_factory))
    monkeypatch.setattr(WorkflowExecutionService, "_dispatch_human_approval", real)
    crashed = state_of(session_factory, harness)
    assert {crashed["nodes"][k] for k in GATE_PARENTS} == {WorkflowNodeRunStatus.COMPLETED}  # all six parents done
    assert crashed["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and crashed["approvals"] == []

    for _ in range(3):
        sweep(session_factory)
    in_threads(5, lambda _i: sweep(session_factory))

    state = state_of(session_factory, harness)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL and len(state["approvals"]) == 1
    assert counts(session_factory) == FINAL_COUNTS
    assert count_events(session_factory, harness.built.task_run.id, "approval.requested") == 1


def test_a_waiting_gate_survives_restarts_and_can_then_be_decided(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "g6")
    state = manual_to_gate(db, session_factory, harness, tmp_path)
    approval = state["approvals"][0]
    before, types_before = counts(session_factory), count_events(session_factory, harness.built.task_run.id, "approval.requested")
    for _ in range(4):
        assert sweep(session_factory) == 1
    after = state_of(session_factory, harness)
    assert after["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL and [a.id for a in after["approvals"]] == [approval.id]
    assert counts(session_factory) == before
    assert count_events(session_factory, harness.built.task_run.id, "approval.requested") == types_before
    assert resolve_via_api(client, auth_headers, approval).status_code == 200
    assert state_of(session_factory, harness)["run"] == WorkflowRunStatus.COMPLETED


def test_an_approved_gate_whose_downstream_was_never_dispatched(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "g7", downstream="post")
    state = manual_to_gate(db, session_factory, harness, tmp_path)
    decide_then_die(session_factory, bootstrap, state["approvals"][0], monkeypatch)
    stopped = state_of(session_factory, harness)
    assert stopped["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED and stopped["nodes"]["post"] == WorkflowNodeRunStatus.PENDING
    before = counts(session_factory)

    for _ in range(3):
        sweep(session_factory)
    in_threads(4, lambda _i: sweep(session_factory))

    after = state_of(session_factory, harness)
    assert after["nodes"]["post"] == WorkflowNodeRunStatus.RUNNING
    assert delta(before, counts(session_factory)) == {
        "tasks": 0, "task_runs": 1, "agent_runs": 1, "evaluation_runs": 0, "jobs": 1, "criterion_results": 0, "approvals": 0,
    }  # the downstream node was dispatched exactly ONCE


def test_a_rejected_gate_whose_failure_propagation_was_interrupted(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "g8", downstream="post")
    state = manual_to_gate(db, session_factory, harness, tmp_path)
    decide_then_die(session_factory, bootstrap, state["approvals"][0], monkeypatch, approve=False)
    crashed = state_of(session_factory, harness)
    assert crashed["nodes"]["gate"] == WorkflowNodeRunStatus.FAILED and crashed["run"] == WorkflowRunStatus.RUNNING
    before = counts(session_factory)

    for _ in range(3):
        sweep(session_factory)
    in_threads(4, lambda _i: sweep(session_factory))

    final = state_of(session_factory, harness)
    assert final["run"] == WorkflowRunStatus.FAILED and final["run_ended_at"] is not None
    assert final["nodes"]["post"] == WorkflowNodeRunStatus.PENDING  # downstream never ran
    assert {final["nodes"][k] for k in GATE_PARENTS} == {WorkflowNodeRunStatus.COMPLETED}
    assert counts(session_factory) == before
    assert count_events(session_factory, harness.built.task_run.id, "workflow.failed") == 1


def test_repeated_and_concurrent_sweeps_and_duplicate_callbacks_change_nothing(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "g9")
    state = manual_to_gate(db, session_factory, harness, tmp_path)
    before = counts(session_factory)
    events_before = count_events(session_factory, harness.built.task_run.id, "workflow.node.completed")
    agent_ids = [state["node_runs"][k].agent_run_id for k in ("planner", "engineer", *REVIEWERS, *EVALS.values())]

    for _ in range(4):
        sweep(session_factory)
    in_threads(5, lambda _i: sweep(session_factory))
    for agent_id in agent_ids:  # every completion callback replayed, twice
        for _ in range(2):
            WorkflowExecutionService(db).on_agent_run_complete(agent_id)

    assert counts(session_factory) == before
    assert count_events(session_factory, harness.built.task_run.id, "workflow.node.completed") == events_before
    assert state_of(session_factory, harness)["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL


# =============================================================================
# H. Races
# =============================================================================


def test_simultaneous_completion_of_the_three_reviewers_dispatches_each_evaluation_exactly_once(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    for round_number in range(6):
        harness = build_parallel(db, bootstrap.project, monkeypatch, f"h1-{round_number}")
        go(db, harness)
        for key in ("planner", "engineer"):
            manual_complete(db, session_factory, harness, key, tmp_path)
        agents = {key: manual_finish_only(db, session_factory, harness, key, tmp_path) for key in REVIEWERS}
        before = counts(session_factory)

        def report(i, agents=agents):
            session = session_factory()
            try:
                WorkflowExecutionService(session).on_agent_run_complete(agents[REVIEWERS[i]].id)
            finally:
                session.close()

        in_threads(3, report)

        assert delta(before, counts(session_factory)) == {
            "tasks": 3, "task_runs": 3, "agent_runs": 3, "evaluation_runs": 3, "jobs": 3, "criterion_results": 0, "approvals": 0,
        }, round_number
        state = state_of(session_factory, harness)
        assert {state["nodes"][e] for e in EVALS.values()} == {WorkflowNodeRunStatus.RUNNING}
        assert len({state["node_runs"][e].agent_run_id for e in EVALS.values()}) == 3


def test_simultaneous_scheduling_of_the_three_evaluations_by_racing_schedulers_and_sweeps(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    for round_number in range(4):
        harness = build_parallel(db, bootstrap.project, monkeypatch, f"h2-{round_number}")
        go(db, harness)
        for key in ("planner", "engineer"):
            manual_complete(db, session_factory, harness, key, tmp_path)
        agents = {key: manual_finish_only(db, session_factory, harness, key, tmp_path) for key in REVIEWERS}
        real = WorkflowExecutionService._dispatch_node_for_execution
        monkeypatch.setattr(WorkflowExecutionService, "_dispatch_node_for_execution", lambda self, nr: None)
        for key in REVIEWERS:  # completed, but every dispatch "died": all three evaluations ready, none dispatched
            WorkflowExecutionService(db).on_agent_run_complete(agents[key].id)
        monkeypatch.setattr(WorkflowExecutionService, "_dispatch_node_for_execution", real)
        assert {state_of(session_factory, harness)["nodes"][e] for e in EVALS.values()} == {WorkflowNodeRunStatus.PENDING}
        before = counts(session_factory)

        def contend(i, run_id=harness.run.id):
            session = session_factory()
            try:
                service = WorkflowExecutionService(session)
                if i % 2 == 0:
                    service._schedule_ready_nodes(run_id)
                else:
                    service.reconcile_workflow_run(run_id)
            finally:
                session.close()

        in_threads(6, contend)

        assert delta(before, counts(session_factory)) == {
            "tasks": 3, "task_runs": 3, "agent_runs": 3, "evaluation_runs": 3, "jobs": 3, "criterion_results": 0, "approvals": 0,
        }, round_number


def test_concurrent_finalization_of_all_three_evaluations(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    from app.db.enums import TaskRunStatus
    from app.services.evaluation_execution_service import EvaluationExecutionService

    harness = build_parallel(db, bootstrap.project, monkeypatch, "h3")
    manual_to_evaluations_running(db, session_factory, harness, tmp_path)
    for node in EVALS.values():  # evaluators terminal (real JSON artifacts); the finalizers have not run
        manual_finish_only(db, session_factory, harness, node, tmp_path)
    runs = list(evaluation_by_node(session_factory, harness).values())
    assert {r.status for r in runs} == {EvaluationRunStatus.RUNNING}

    def finalize(i):
        session = session_factory()
        try:
            EvaluationExecutionService(session).finalize_agent_evaluator_run(
                runs[i % 3].id, task_run_status=TaskRunStatus.COMPLETED
            )
        finally:
            session.close()

    in_threads(9, finalize)  # three finalizers race for EACH evaluation

    final = evaluation_by_node(session_factory, harness)
    assert {e.status for e in final.values()} == {EvaluationRunStatus.COMPLETED}
    assert counts(session_factory)["criterion_results"] == 6  # one result set per evaluation, never two
    # and exactly ONE finalizer per evaluation acted: one terminal event, no spurious failure from a loser
    for evaluation in final.values():
        assert count_events(session_factory, evaluation.evaluator_task_run_id, "evaluation_run.completed") == 1
        assert count_events(session_factory, evaluation.evaluator_task_run_id, "evaluation_run.failed") == 0


def test_approval_creation_race_after_the_last_two_parents_complete_together(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    for round_number in range(6):
        harness = build_parallel(db, bootstrap.project, monkeypatch, f"h4-{round_number}")
        manual_to_evaluations_running(db, session_factory, harness, tmp_path)
        manual_complete(db, session_factory, harness, "eval_code", tmp_path)
        agents = [manual_finish_only(db, session_factory, harness, node, tmp_path) for node in ("eval_security", "eval_test")]
        before = counts(session_factory)

        def report(i, agents=agents):
            session = session_factory()
            try:
                WorkflowExecutionService(session).on_agent_run_complete(agents[i].id)
            finally:
                session.close()

        in_threads(2, report)

        state = state_of(session_factory, harness)
        assert len(state["approvals"]) == 1 and state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL, round_number
        assert delta(before, counts(session_factory))["approvals"] == 1
        assert count_events(session_factory, harness.built.task_run.id, "approval.requested") == 1


def test_approval_and_reconciliation_race_resumes_exactly_once(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    for round_number in range(4):
        harness = build_parallel(db, bootstrap.project, monkeypatch, f"h5-{round_number}", downstream="post")
        state = manual_to_gate(db, session_factory, harness, tmp_path)
        approval = state["approvals"][0]
        before = counts(session_factory)

        def act(i, approval=approval):
            session = session_factory()
            try:
                if i == 0:
                    ApprovalService(session).resolve(
                        approval.id, user=bootstrap.user, approve=True, action_fingerprint=approval.action_fingerprint
                    )
                else:
                    WorkflowExecutionService(session).reconcile_active_runs()
            finally:
                session.close()

        in_threads(5, act)

        final = state_of(session_factory, harness)
        assert final["approvals"][0].status == ApprovalStatus.APPROVED, round_number
        assert final["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED and final["nodes"]["post"] == WorkflowNodeRunStatus.RUNNING
        assert delta(before, counts(session_factory))["agent_runs"] == 1  # 'post' dispatched exactly once


def test_approval_and_cancellation_race_never_leaves_a_contradictory_state(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    for round_number in range(5):
        harness = build_parallel(db, bootstrap.project, monkeypatch, f"h6-{round_number}", downstream="post")
        state = manual_to_gate(db, session_factory, harness, tmp_path)
        approval = state["approvals"][0]

        def act(i, approval=approval, run_id=harness.run.id):
            session = session_factory()
            try:
                if i == 0:
                    try:
                        ApprovalService(session).resolve(
                            approval.id, user=bootstrap.user, approve=True, action_fingerprint=approval.action_fingerprint
                        )
                    except InvalidStateTransitionError:
                        pass  # the cancellation won
                else:
                    WorkflowExecutionService(session).cancel_workflow_run(run_id, cancelled_by_user_id=bootstrap.user.id)
            finally:
                session.close()

        in_threads(2, act)

        final = state_of(session_factory, harness)
        assert (final["approvals"][0].status, final["nodes"]["gate"]) in {
            (ApprovalStatus.APPROVED, WorkflowNodeRunStatus.COMPLETED),
            (ApprovalStatus.EXPIRED, WorkflowNodeRunStatus.CANCELLED),
        }, round_number
        assert final["run"] in (WorkflowRunStatus.CANCELLING, WorkflowRunStatus.CANCELLED)
        if final["nodes"]["post"] == WorkflowNodeRunStatus.RUNNING:  # dispatched into a cancelling run: must be signalled
            assert _requested(db, final["node_runs"]["post"].agent_run_id)


def test_evaluation_failure_and_workflow_cancellation_race(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    for round_number in range(5):
        harness = build_parallel(db, bootstrap.project, monkeypatch, f"h7-{round_number}")
        manual_to_evaluations_running(db, session_factory, harness, tmp_path)
        failing = manual_finish_only(db, session_factory, harness, "eval_security", tmp_path, status=AgentRunStatus.FAILED)

        def act(i, failing=failing, run_id=harness.run.id):
            session = session_factory()
            try:
                if i == 0:
                    WorkflowExecutionService(session).on_agent_run_complete(failing.id)
                else:
                    WorkflowExecutionService(session).cancel_workflow_run(run_id, cancelled_by_user_id=bootstrap.user.id)
            finally:
                session.close()

        in_threads(2, act)

        mid = state_of(session_factory, harness)
        assert mid["nodes"]["eval_security"] == WorkflowNodeRunStatus.FAILED and mid["nodes"]["gate"] != WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
        assert mid["approvals"] == []
        for sibling in ("eval_code", "eval_test"):  # both siblings were told to stop, whichever won the race
            assert _requested(db, mid["node_runs"][sibling].agent_run_id)
        for sibling in ("eval_code", "eval_test"):
            manual_complete(db, session_factory, harness, sibling, tmp_path, status=AgentRunStatus.STOPPED)

        final = state_of(session_factory, harness)
        assert final["run"] == WorkflowRunStatus.FAILED  # failure outranks cancellation
        assert final["approvals"] == [] and {e.status for e in rows(session_factory, EvaluationRun)} <= {
            EvaluationRunStatus.COMPLETED,
            EvaluationRunStatus.FAILED,
            EvaluationRunStatus.CANCELLED,
        }
        assert count_events(session_factory, harness.built.task_run.id, "workflow.failed") == 1


# =============================================================================
# I. Read model
# =============================================================================


def test_each_evaluation_node_exposes_its_own_evaluation_run_id_and_nothing_aggregates(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "i1")
    state = manual_to_gate(db, session_factory, harness, tmp_path)
    by_agent = {e.evaluator_agent_run_id: e.id for e in rows(session_factory, EvaluationRun)}

    nodes = {n["workflow_node_id"]: n for n in client.get(f"/workflow-runs/{harness.run.id}/nodes", headers=auth_headers).json()}
    by_key = {key: nodes[harness.built.nodes[key].id] for key in harness.built.nodes}
    exposed = {key: by_key[key]["evaluation_run_id"] for key in EVALS.values()}
    assert len(set(exposed.values())) == 3 and None not in exposed.values()  # three DISTINCT ids
    for key, evaluation_run_id in exposed.items():
        assert evaluation_run_id == by_agent[state["node_runs"][key].agent_run_id]
    assert all(
        by_key[k]["evaluation_run_id"] is None for k in ("planner", "engineer", *REVIEWERS, "gate", "result")
    )

    # no aggregate anywhere: not on the run, not on the gate's approval, not in its evidence
    run_body = client.get(f"/workflow-runs/{harness.run.id}", headers=auth_headers).json()
    approval_id = state["approvals"][0].id
    approval_body = client.get(f"/approvals/{approval_id}", headers=auth_headers).json()
    evidence_body = get_evidence(client, auth_headers, approval_id).json()
    for body in (run_body, approval_body, evidence_body):
        assert "evaluation_run_id" not in body and "evaluation_run_ids" not in body
    assert all("evaluation_run_id" not in e for e in evidence_body["evidence"])


def test_no_score_rank_winner_or_aggregate_exists_in_any_evaluation_read(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "i2", findings={"eval_test": {"security": "NOT_MET"}})
    manual_to_gate(db, session_factory, harness, tmp_path)
    for evaluation in rows(session_factory, EvaluationRun):
        body = client.get(f"/evaluation-runs/{evaluation.id}", headers=auth_headers).json()
        forbidden = {"score", "rank", "ranking", "winner", "aggregate", "overall", "verdict", "decision", "approved"}
        assert not forbidden & set(body) and all(not forbidden & set(r) for r in body["criterion_results"])
        assert {r["finding"] for r in body["criterion_results"]} <= {"met", "partial", "not_met", "not_applicable"}

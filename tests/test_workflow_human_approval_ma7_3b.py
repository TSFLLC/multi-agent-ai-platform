"""MA7.3b -- Human Approval nodes inside the MA7 workflow engine.

Behavior suite. The flagship production-path acceptance test (real Worker +
real AgentExecutionService) is in test_workflow_human_approval_ma7_3b_e2e.py;
here only the inference boundary (``AgentExecutionService.execute``) is stubbed
-- the real Worker, reconciliation, dispatcher, ApprovalService and HTTP API run.
Crash windows are simulated by making the exact step after the crash point
fail once, then recovering through the real reconciliation sweep. Disposable
temp databases only.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.db.enums import (
    ApprovalScope,
    ApprovalStatus,
    ProjectRole,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
    WorkflowRunStatus,
)
from app.errors import ConflictError, InvalidStateTransitionError
from app.models.identity import Project, ProjectMembership
from app.models.workflow import WorkflowNodeRun, WorkflowRun
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.approval_service import WORKFLOW_HUMAN_APPROVAL_OPERATION, ApprovalService
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.workflow_execution_service import WorkflowExecutionService
from app.services.workflow_validation_service import DAGValidationError
from app.worker import Worker
from tests.ma7_3b_support import (
    AGENT,
    APPROVAL,
    TERMINAL,
    audit_types,
    build_chain,
    drain,
    event_types,
    events,
    install_fake_agents,
    resolve_via_api,
    snapshot,
    start,
)

PLANNER_FIRST = [("planner", AGENT), ("gate", APPROVAL), ("engineer", AGENT), ("result", TERMINAL)]


@pytest.fixture()
def calls(monkeypatch, tmp_path):
    return install_fake_agents(monkeypatch, tmp_path)


@pytest.fixture()
def worker(session_factory):
    return Worker(
        session_factory=session_factory,
        poll_interval_seconds=0.01,
        lease_seconds=5,
        heartbeat_interval_seconds=0.05,
    )


def waiting_run(db, session_factory, bootstrap, worker, layout=PLANNER_FIRST, label="wf", project=None):
    """Build + start + let the real Worker run every agent node up to the gate."""
    built = build_chain(db, project or bootstrap.project, layout, label=label)
    run = start(db, built)
    drain(worker)
    return built, run


def the_approval(session_factory, run_id, index=0):
    return snapshot(session_factory, run_id)["approvals"][index]


def decide_then_die(session_factory, bootstrap, approval, monkeypatch, *, approve=True):
    """Commits a decision (atomically with the node/run transition) and then
    the process 'dies': the post-commit resume that would dispatch the next
    node never runs. Restores the real resume afterwards, so recovery can be
    exercised through the real reconciliation."""
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


# --- dispatch: a durable wait, never an execution ---------------------------


def test_gate_becomes_a_durable_wait_with_no_execution_artifacts(
    db, session_factory, bootstrap, worker, calls
):
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    state = snapshot(session_factory, run.id)

    assert calls == ["planner"]
    assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert state["nodes"]["engineer"] == WorkflowNodeRunStatus.PENDING
    gate_run = state["node_runs"]["gate"]
    assert gate_run.agent_run_id is None and gate_run.started_at is not None and gate_run.ended_at is None

    # exactly one PENDING approval, system-requested, bound to the Planner's artifact
    (approval,) = state["approvals"]
    assert approval.status == ApprovalStatus.PENDING
    assert approval.scope == ApprovalScope.WORKFLOW_NODE_RUN and approval.scope_ref_id == gate_run.id
    assert approval.operation_type == WORKFLOW_HUMAN_APPROVAL_OPERATION
    assert approval.requested_by is None and approval.expires_at is None
    assert len(approval.action_fingerprint) == 64
    assert approval.bound_artifact_id == state["node_runs"]["planner"].output_snapshot_ref

    # zero executions for the gate: one AgentRun/job (Planner) only
    assert (state["agent_runs_total"], state["jobs_total"]) == (1, 1)
    assert worker.run_once() is False


def test_gate_dispatch_emits_flight_recorder_evidence(db, session_factory, bootstrap, worker, calls):
    built, run = waiting_run(db, session_factory, bootstrap, worker)
    gate_run_id = snapshot(session_factory, run.id)["node_runs"]["gate"].id
    listed = events(session_factory, built.task_run.id)

    by_type = {e[0]: e for e in listed}
    assert "workflow.node.waiting_for_approval" in by_type and "approval.requested" in by_type
    for name in ("workflow.node.waiting_for_approval", "approval.requested"):
        _type, actor_type, actor_user_id, node_run_id = by_type[name]
        assert (actor_type, actor_user_id, node_run_id) == ("system", None, gate_run_id)


def test_gate_as_the_entry_node_waits_immediately(db, session_factory, bootstrap, worker, calls):
    built = build_chain(
        db, bootstrap.project, [("gate", APPROVAL), ("engineer", AGENT), ("result", TERMINAL)], label="entry"
    )
    run = start(db, built)
    state = snapshot(session_factory, run.id)

    assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    (approval,) = state["approvals"]
    assert approval.bound_artifact_id is None  # nothing upstream to bind
    assert (state["agent_runs_total"], state["jobs_total"]) == (0, 0)
    assert worker.run_once() is False and calls == []


def test_node_runs_api_exposes_the_gates_approval_id(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)

    nodes = client.get(f"/workflow-runs/{run.id}/nodes", headers=auth_headers).json()
    with_approval = [n for n in nodes if n["approval_id"] is not None]
    assert [(n["status"], n["approval_id"]) for n in with_approval] == [("waiting_for_approval", approval.id)]
    assert all(n["approval_id"] is None for n in nodes if n not in with_approval)


# --- APPROVE / three-gate ---------------------------------------------------


def test_three_gate_workflow_runs_in_order_and_passes_artifacts_through_each_gate(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    layout = [
        ("planner", AGENT),
        ("gate1", APPROVAL),
        ("engineer", AGENT),
        ("gate2", APPROVAL),
        ("reviewer", AGENT),
        ("result", TERMINAL),
    ]
    _built, run = waiting_run(db, session_factory, bootstrap, worker, layout, label="three")
    assert calls == ["planner"]

    first = the_approval(session_factory, run.id)
    assert resolve_via_api(client, auth_headers, first, notes="ok 1").status_code == 200
    drain(worker)
    assert calls == ["planner", "engineer"]

    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert state["nodes"]["gate2"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert state["nodes"]["reviewer"] == WorkflowNodeRunStatus.PENDING
    assert len(state["approvals"]) == 2  # one per gate, no duplicates
    second = next(a for a in state["approvals"] if a.id != first.id)
    assert second.status == ApprovalStatus.PENDING

    assert resolve_via_api(client, auth_headers, second, notes="ok 2").status_code == 200
    drain(worker)

    final = snapshot(session_factory, run.id)
    assert calls == ["planner", "engineer", "reviewer"]
    assert final["run"] == WorkflowRunStatus.COMPLETED
    assert set(final["nodes"].values()) == {WorkflowNodeRunStatus.COMPLETED}
    assert (final["agent_runs_total"], final["jobs_total"]) == (3, 3)
    assert len(final["approvals"]) == 2
    assert all(a.status == ApprovalStatus.APPROVED for a in final["approvals"])

    # each gate passed its single upstream artifact straight through
    nr = final["node_runs"]
    assert nr["gate1"].output_snapshot_ref == nr["planner"].output_snapshot_ref
    assert nr["gate2"].output_snapshot_ref == nr["engineer"].output_snapshot_ref
    verify = session_factory()
    try:
        from app.models.tasks import AgentRun

        engineer_ctx = verify.get(AgentRun, nr["engineer"].agent_run_id).input_context_json
        reviewer_ctx = verify.get(AgentRun, nr["reviewer"].agent_run_id).input_context_json
    finally:
        verify.close()
    assert engineer_ctx["upstream_artifact_ids"] == [nr["planner"].output_snapshot_ref]
    assert reviewer_ctx["upstream_artifact_ids"] == [nr["engineer"].output_snapshot_ref]
    assert reviewer_ctx["upstream_node_run_ids"] == [nr["gate2"].id]


# --- REJECT -----------------------------------------------------------------


def test_reject_fails_the_run_and_downstream_agents_never_execute(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)

    response = resolve_via_api(client, auth_headers, approval, approve=False, notes="wrong approach")
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["resolved_by"]) == ("rejected", bootstrap.user.id)

    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.FAILED and state["run_ended_at"] is not None
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.FAILED
    assert state["nodes"]["engineer"] == WorkflowNodeRunStatus.PENDING  # never dispatched
    assert state["node_runs"]["engineer"].agent_run_id is None
    assert state["approvals"][0].status == ApprovalStatus.REJECTED
    assert state["approvals"][0].resolution_note == "wrong approach"

    assert worker.run_once() is False
    assert calls == ["planner"]
    assert (state["agent_runs_total"], state["jobs_total"]) == (1, 1)

    listed = events(session_factory, built.task_run.id)
    types = [e[0] for e in listed]
    for expected in ("approval.rejected", "workflow.node.failed", "workflow.failed"):
        assert expected in types
    rejected = next(e for e in listed if e[0] == "approval.rejected")
    assert (rejected[1], rejected[2]) == ("user", bootstrap.user.id)
    assert "workflow.completed" not in types and "approval.approved" not in types
    assert audit_types(session_factory) == ["approval.rejected"]


def test_reject_is_idempotent_and_a_later_approve_conflicts_without_effect(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)

    assert resolve_via_api(client, auth_headers, approval, approve=False).status_code == 200
    assert resolve_via_api(client, auth_headers, approval, approve=False).status_code == 200  # replay
    flip = resolve_via_api(client, auth_headers, approval, approve=True)
    assert flip.status_code == 409 and flip.json()["error"]["code"] == "invalid_state_transition"

    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.FAILED
    assert state["approvals"][0].status == ApprovalStatus.REJECTED
    assert worker.run_once() is False and calls == ["planner"]
    assert audit_types(session_factory) == ["approval.rejected"]


# --- duplicate / concurrent resolution --------------------------------------


def test_duplicate_approve_dispatches_the_engineer_exactly_once(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)

    statuses = [resolve_via_api(client, auth_headers, approval).status_code for _ in range(3)]
    assert statuses == [200, 200, 200]
    assert resolve_via_api(client, auth_headers, approval, approve=False).status_code == 409

    state = snapshot(session_factory, run.id)
    assert (state["agent_runs_total"], state["jobs_total"]) == (2, 2)  # Planner + ONE Engineer
    drain(worker)
    assert calls == ["planner", "engineer"]
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED
    assert audit_types(session_factory) == ["approval.approved"]  # only the winner is audited


def test_concurrent_approvals_yield_one_decision_and_one_engineer(
    session_factory, db, bootstrap, worker, calls
):
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)
    barrier = threading.Barrier(6)

    def attempt(_):
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            return (
                ApprovalService(session)
                .resolve(
                    approval.id,
                    user=bootstrap.user,
                    approve=True,
                    action_fingerprint=approval.action_fingerprint,
                )
                .status
            )
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(attempt, range(6))) == [ApprovalStatus.APPROVED] * 6

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED
    # no duplicate Engineer AgentRun/TaskRun/job -- and no orphan rows from lost dispatch races
    assert (state["agent_runs_total"], state["task_runs_total"], state["jobs_total"]) == (2, 3, 2)
    drain(worker)
    assert calls == ["planner", "engineer"]
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED
    assert audit_types(session_factory) == ["approval.approved"]


def test_concurrent_approve_and_reject_exactly_one_wins_and_run_is_consistent(
    session_factory, db, bootstrap, worker, calls
):
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)
    barrier = threading.Barrier(4)

    def attempt(approve):
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            return (
                ApprovalService(session)
                .resolve(
                    approval.id,
                    user=bootstrap.user,
                    approve=approve,
                    action_fingerprint=approval.action_fingerprint,
                )
                .status
            )
        except InvalidStateTransitionError:
            return "conflict"
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(attempt, [True, False, True, False]))

    state = snapshot(session_factory, run.id)
    winner = state["approvals"][0].status
    assert winner in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
    assert set(outcomes) <= {winner, "conflict"} and winner in outcomes
    if winner == ApprovalStatus.APPROVED:
        assert state["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED
        assert state["agent_runs_total"] == 2
    else:
        assert state["nodes"]["gate"] == WorkflowNodeRunStatus.FAILED
        assert state["run"] == WorkflowRunStatus.FAILED and state["agent_runs_total"] == 1


def test_lost_dispatch_race_leaves_no_orphan_agent_or_task_runs(db, session_factory, bootstrap, calls):
    """MA7.2 defect fixed in MA7.3b: the losing dispatcher used to COMMIT its
    flushed TaskRun/AgentRun. Reachable now that concurrent approve replays
    and simultaneous startup sweeps can dispatch the same ready node."""
    built = build_chain(db, bootstrap.project, [("only", AGENT), ("result", TERMINAL)], label="race")
    run = start(db, built)
    baseline = snapshot(session_factory, run.id)
    db.execute(
        text(
            "UPDATE workflow_node_runs SET status='pending', agent_run_id=NULL WHERE workflow_run_id=:r AND status='running'"
        ),
        {"r": run.id},
    )
    db.execute(text("DELETE FROM job_queue"))
    db.commit()
    barrier = threading.Barrier(2)

    def dispatch(_):
        session = session_factory()
        try:
            node_run = (
                session.query(WorkflowNodeRun)
                .filter(
                    WorkflowNodeRun.workflow_run_id == run.id,
                    WorkflowNodeRun.workflow_node_id == built.nodes["only"].id,
                    WorkflowNodeRun.status == WorkflowNodeRunStatus.PENDING,
                )
                .first()
            )
            barrier.wait(timeout=10)
            WorkflowExecutionService(session)._dispatch_node_for_execution(node_run)
            session.commit()
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(dispatch, range(2)))

    after = snapshot(session_factory, run.id)
    # baseline already holds the first dispatch's rows; the re-dispatch adds exactly ONE pair
    assert after["agent_runs_total"] == baseline["agent_runs_total"] + 1
    assert after["task_runs_total"] == baseline["task_runs_total"] + 1
    assert after["jobs_total"] == 1


@pytest.mark.parametrize("run_status", ["cancelling", "failed", "cancelled", "completed"])
def test_scheduler_never_dispatches_a_ready_node_for_a_run_that_is_not_running(
    db, session_factory, bootstrap, worker, calls, monkeypatch, run_status
):
    """Defense in depth behind the callers' own checks: an approve that raced a
    cancellation (or a run that is terminal) must never start the next agent.

    (MA7.4b: ``node_waiting_for_approval`` used to be in this list. It is a
    SUMMARY status, not a lock -- an independent branch that is ready while a
    gate waits must still be dispatched -- so it is asserted the other way
    round in tests/test_ma7_4b_parallel_execution.py.)"""
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    decide_then_die(session_factory, bootstrap, the_approval(session_factory, run.id), monkeypatch)
    ready = snapshot(session_factory, run.id)
    assert ready["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED
    assert ready["nodes"]["engineer"] == WorkflowNodeRunStatus.PENDING  # ready, not yet dispatched
    db.execute(text("UPDATE workflow_runs SET status=:s WHERE id=:id"), {"s": run_status, "id": run.id})
    db.commit()

    session = session_factory()
    try:
        WorkflowExecutionService(session)._schedule_ready_nodes(run.id)
    finally:
        session.close()

    after = snapshot(session_factory, run.id)
    assert after["nodes"]["engineer"] == WorkflowNodeRunStatus.PENDING
    assert (after["agent_runs_total"], after["jobs_total"]) == (1, 1)


def test_concurrent_dispatch_of_one_gate_creates_one_approval_and_one_wait(
    db, session_factory, bootstrap, worker, calls, monkeypatch
):
    built = build_chain(db, bootstrap.project, PLANNER_FIRST, label="gaterace")
    run = start(db, built)
    real_dispatch = WorkflowExecutionService._dispatch_human_approval
    monkeypatch.setattr(
        WorkflowExecutionService,
        "_dispatch_human_approval",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("process died")),
    )
    drain(worker)  # Planner completes; the gate is left PENDING and ready
    monkeypatch.setattr(WorkflowExecutionService, "_dispatch_human_approval", real_dispatch)
    gate_id = built.nodes["gate"].id
    barrier = threading.Barrier(4)

    def dispatch(_):
        session = session_factory()
        try:
            node_run = (
                session.query(WorkflowNodeRun)
                .filter(
                    WorkflowNodeRun.workflow_run_id == run.id, WorkflowNodeRun.workflow_node_id == gate_id
                )
                .first()
            )
            barrier.wait(timeout=10)
            WorkflowExecutionService(session)._dispatch_node_for_execution(node_run)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(dispatch, range(4)))

    state = snapshot(session_factory, run.id)
    assert len(state["approvals"]) == 1
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    types = event_types(session_factory, built.task_run.id)
    assert types.count("workflow.node.waiting_for_approval") == 1
    assert types.count("approval.requested") == 1
    assert (state["agent_runs_total"], state["jobs_total"]) == (1, 1)


# --- CANCEL -----------------------------------------------------------------


def test_cancel_while_waiting_expires_the_approval_and_cancels_the_run(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)

    response = client.post(f"/workflow-runs/{run.id}/cancel", headers=auth_headers)
    assert response.status_code == 200

    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.CANCELLED and state["run_ended_at"] is not None
    assert state["nodes"] == {
        "planner": WorkflowNodeRunStatus.COMPLETED,  # completed work is preserved
        "gate": WorkflowNodeRunStatus.CANCELLED,
        "engineer": WorkflowNodeRunStatus.CANCELLED,
        "result": WorkflowNodeRunStatus.CANCELLED,
    }
    closed = state["approvals"][0]
    # cancellation is NOT a rejection: EXPIRED, reason recorded, canceller preserved
    assert closed.status == ApprovalStatus.EXPIRED
    assert closed.resolution_note == "workflow_cancelled"
    assert closed.resolved_by == bootstrap.user.id and closed.resolved_at is not None

    check = session_factory()
    try:
        assert check.get(WorkflowRun, run.id).cancellation_requested_by == bootstrap.user.id
    finally:
        check.close()

    assert worker.run_once() is False and calls == ["planner"]
    assert state["agent_runs_total"] == 1

    listed = events(session_factory, built.task_run.id)
    cancelled = next(e for e in listed if e[0] == "approval.cancelled")
    assert (cancelled[1], cancelled[2]) == ("user", bootstrap.user.id)
    assert "approval.rejected" not in [e[0] for e in listed]
    assert audit_types(session_factory) == ["approval.cancelled"]

    # the approval can no longer be decided
    late = resolve_via_api(client, auth_headers, approval, approve=True)
    assert late.status_code == 409
    assert snapshot(session_factory, run.id)["approvals"][0].status == ApprovalStatus.EXPIRED


def test_cancel_is_idempotent(client, db, session_factory, auth_headers, bootstrap, worker, calls):
    built, run = waiting_run(db, session_factory, bootstrap, worker)
    assert client.post(f"/workflow-runs/{run.id}/cancel", headers=auth_headers).status_code == 200
    first = (snapshot(session_factory, run.id)["run"], len(events(session_factory, built.task_run.id)))
    assert client.post(f"/workflow-runs/{run.id}/cancel", headers=auth_headers).status_code == 200
    assert (
        snapshot(session_factory, run.id)["run"],
        len(events(session_factory, built.task_run.id)),
    ) == first
    assert audit_types(session_factory) == ["approval.cancelled"]


def test_cancel_route_records_the_cancelling_user(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    """Pre-existing MA7.2 gap: the route passed ``cancelled_by_user_id=None``."""
    built = build_chain(db, bootstrap.project, [("only", AGENT), ("result", TERMINAL)], label="who")
    run = start(db, built)  # node dispatched and running, no worker yet
    assert client.post(f"/workflow-runs/{run.id}/cancel", headers=auth_headers).status_code == 200
    check = session_factory()
    try:
        assert check.get(WorkflowRun, run.id).cancellation_requested_by == bootstrap.user.id
    finally:
        check.close()


@pytest.mark.parametrize("round_number", range(10))
def test_approve_versus_cancel_race_always_ends_consistent(
    session_factory, db, bootstrap, worker, calls, round_number
):
    _built, run = waiting_run(db, session_factory, bootstrap, worker, label=f"race{round_number}")
    approval = the_approval(session_factory, run.id)
    barrier = threading.Barrier(2)

    def approve():
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            ApprovalService(session).resolve(
                approval.id, user=bootstrap.user, approve=True, action_fingerprint=approval.action_fingerprint
            )
            return "approved"
        except (InvalidStateTransitionError, ConflictError):
            return "refused"
        finally:
            session.close()

    def cancel():
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            WorkflowExecutionService(session).cancel_workflow_run(
                run.id, cancelled_by_user_id=bootstrap.user.id
            )
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        approve_future, cancel_future = pool.submit(approve), pool.submit(cancel)
        outcome = approve_future.result(timeout=30)
        cancel_future.result(timeout=30)

    state = snapshot(session_factory, run.id)
    approval_after = state["approvals"][0]
    assert approval_after.status != ApprovalStatus.REJECTED  # a cancellation is never a rejection
    assert state["jobs_total"] == state["agent_runs_total"] <= 2  # never more than one Engineer

    if approval_after.status == ApprovalStatus.APPROVED:
        assert outcome == "approved"
        assert state["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED
        # the Engineer was either dispatched once (then cancelled while in flight) or never started
        if state["agent_runs_total"] == 2:
            assert state["nodes"]["engineer"] == WorkflowNodeRunStatus.RUNNING
        else:
            assert state["nodes"]["engineer"] == WorkflowNodeRunStatus.CANCELLED
        assert state["run"] in (WorkflowRunStatus.CANCELLING, WorkflowRunStatus.CANCELLED)
    else:
        assert approval_after.status == ApprovalStatus.EXPIRED and outcome == "refused"
        assert approval_after.resolution_note == "workflow_cancelled"
        assert state["nodes"]["gate"] == WorkflowNodeRunStatus.CANCELLED
        assert state["run"] == WorkflowRunStatus.CANCELLED and state["agent_runs_total"] == 1


# --- restart / crash recovery ------------------------------------------------


def _sweep(session_factory):
    session = session_factory()
    try:
        return WorkflowExecutionService(session).reconcile_active_runs()
    finally:
        session.close()


def test_planner_completed_but_process_stopped_before_the_approval_was_created(
    db, session_factory, bootstrap, worker, calls, monkeypatch
):
    built = build_chain(db, bootstrap.project, PLANNER_FIRST, label="crash1")
    run = start(db, built)

    # the process "dies" at the exact step after the Planner's completion commit
    real_dispatch = WorkflowExecutionService._dispatch_human_approval
    monkeypatch.setattr(
        WorkflowExecutionService,
        "_dispatch_human_approval",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("process died")),
    )
    drain(worker)
    crashed = snapshot(session_factory, run.id)
    assert crashed["nodes"]["planner"] == WorkflowNodeRunStatus.COMPLETED
    assert crashed["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING
    assert crashed["run"] == WorkflowRunStatus.RUNNING and crashed["approvals"] == []

    monkeypatch.setattr(WorkflowExecutionService, "_dispatch_human_approval", real_dispatch)
    for _ in range(3):  # repeated recovery converges, never duplicates
        assert _sweep(session_factory) == 1
        state = snapshot(session_factory, run.id)
        assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
        assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
        assert len(state["approvals"]) == 1 and state["approvals"][0].status == ApprovalStatus.PENDING
        assert (state["agent_runs_total"], state["jobs_total"]) == (1, 1)
    assert worker.run_once() is False and calls == ["planner"]


def test_approved_but_process_stopped_before_downstream_dispatch(
    db, session_factory, bootstrap, worker, calls, monkeypatch
):
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)

    # decision + node/run transition commit atomically; the process then dies
    # before its post-commit resume ever runs
    decide_then_die(session_factory, bootstrap, approval, monkeypatch)

    stopped = snapshot(session_factory, run.id)
    assert stopped["approvals"][0].status == ApprovalStatus.APPROVED
    assert stopped["run"] == WorkflowRunStatus.RUNNING
    assert stopped["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED
    assert stopped["nodes"]["engineer"] == WorkflowNodeRunStatus.PENDING
    assert (stopped["agent_runs_total"], stopped["jobs_total"]) == (1, 1)

    for _ in range(3):
        assert _sweep(session_factory) == 1
        state = snapshot(session_factory, run.id)
        assert state["nodes"]["engineer"] == WorkflowNodeRunStatus.RUNNING
        assert (state["agent_runs_total"], state["jobs_total"]) == (2, 2)  # dispatched ONCE
    drain(worker)
    assert calls == ["planner", "engineer"]
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED


@pytest.mark.parametrize("approve", [True, False])
def test_resolved_approval_whose_node_was_never_advanced_is_applied_by_recovery(
    db, session_factory, bootstrap, worker, calls, approve
):
    """State a two-step (non-atomic) writer could leave behind; the sweep must
    still converge it."""
    built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)
    db.execute(
        text("UPDATE approvals SET status=:s, resolved_by=:u, resolved_at=CURRENT_TIMESTAMP WHERE id=:id"),
        {"s": "approved" if approve else "rejected", "u": bootstrap.user.id, "id": approval.id},
    )
    db.commit()
    before = snapshot(session_factory, run.id)
    assert before["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL

    for _ in range(2):  # the second sweep must be a no-op
        _sweep(session_factory)
    state = snapshot(session_factory, run.id)
    if approve:
        assert state["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED
        assert state["nodes"]["engineer"] == WorkflowNodeRunStatus.RUNNING
        assert (state["agent_runs_total"], state["jobs_total"]) == (2, 2)
        drain(worker)
        assert calls == ["planner", "engineer"]
    else:
        assert state["nodes"]["gate"] == WorkflowNodeRunStatus.FAILED
        assert state["run"] == WorkflowRunStatus.FAILED
        assert (state["agent_runs_total"], state["jobs_total"]) == (1, 1)
    recovered = [e for e in events(session_factory, built.task_run.id) if e[0].startswith("approval.")]
    assert recovered[-1][0] == ("approval.approved" if approve else "approval.rejected")


def test_engineer_claimed_but_its_job_was_never_enqueued_is_repaired(
    client, db, session_factory, auth_headers, bootstrap, worker, calls, monkeypatch
):
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)

    real_enqueue = JobQueueRepository.enqueue
    monkeypatch.setattr(
        JobQueueRepository,
        "enqueue",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("died before enqueue")),
    )
    assert (
        resolve_via_api(client, auth_headers, approval).status_code == 200
    )  # decision is durable regardless
    monkeypatch.setattr(JobQueueRepository, "enqueue", real_enqueue)

    stranded = snapshot(session_factory, run.id)
    assert stranded["nodes"]["engineer"] == WorkflowNodeRunStatus.RUNNING
    assert stranded["agent_runs_total"] == 2 and stranded["jobs_total"] == 1  # Engineer has no job

    for _ in range(3):
        _sweep(session_factory)
        state = snapshot(session_factory, run.id)
        assert (state["agent_runs_total"], state["jobs_total"]) == (2, 2)  # one job, however often it runs
    drain(worker)
    assert calls == ["planner", "engineer"]
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED


def test_replaying_an_approve_after_a_crash_resumes_the_run(
    client, db, session_factory, auth_headers, bootstrap, worker, calls, monkeypatch
):
    """A browser retry after the first attempt died post-commit heals the run."""
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)
    real = WorkflowExecutionService.after_approval_resolved
    monkeypatch.setattr(WorkflowExecutionService, "after_approval_resolved", lambda self, *a, **k: None)
    assert resolve_via_api(client, auth_headers, approval).status_code == 200
    assert snapshot(session_factory, run.id)["agent_runs_total"] == 1  # never resumed

    monkeypatch.setattr(WorkflowExecutionService, "after_approval_resolved", real)
    assert resolve_via_api(client, auth_headers, approval).status_code == 200  # idempotent replay
    assert snapshot(session_factory, run.id)["agent_runs_total"] == 2
    assert snapshot(session_factory, run.id)["jobs_total"] == 2


def test_reconciling_a_healthy_waiting_run_changes_nothing(db, session_factory, bootstrap, worker, calls):
    built, run = waiting_run(db, session_factory, bootstrap, worker)
    before = snapshot(session_factory, run.id)
    types_before = event_types(session_factory, built.task_run.id)

    for _ in range(5):
        assert _sweep(session_factory) == 1

    after = snapshot(session_factory, run.id)
    assert after["run"] == before["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert after["nodes"] == before["nodes"]
    assert [a.id for a in after["approvals"]] == [a.id for a in before["approvals"]]
    assert after["approvals"][0].status == ApprovalStatus.PENDING
    assert (after["agent_runs_total"], after["jobs_total"]) == (
        before["agent_runs_total"],
        before["jobs_total"],
    )
    assert event_types(session_factory, built.task_run.id) == types_before  # no duplicate evidence


def test_sweep_ignores_terminal_runs_and_an_empty_database(db, session_factory, bootstrap, worker, calls):
    assert _sweep(session_factory) == 0
    built = build_chain(db, bootstrap.project, [("only", AGENT), ("result", TERMINAL)], label="done")
    run = start(db, built)
    drain(worker)
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED
    assert _sweep(session_factory) == 0


def test_concurrent_sweeps_from_two_processes_create_nothing_duplicate(
    db, session_factory, bootstrap, worker, calls, monkeypatch
):
    """Two startup sweeps (FastAPI + Worker) racing over a run whose approval
    was decided but never resumed."""
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)
    decide_then_die(session_factory, bootstrap, approval, monkeypatch)

    barrier = threading.Barrier(4)

    def sweep(_):
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            return WorkflowExecutionService(session).reconcile_active_runs()
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(sweep, range(4))) == [1, 1, 1, 1]

    state = snapshot(session_factory, run.id)
    assert (state["agent_runs_total"], state["task_runs_total"], state["jobs_total"]) == (2, 3, 2)
    assert len(state["approvals"]) == 1


def test_worker_startup_runs_the_recovery_sweep(db, session_factory, bootstrap, worker, calls, monkeypatch):
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)
    decide_then_die(session_factory, bootstrap, approval, monkeypatch)
    assert snapshot(session_factory, run.id)["agent_runs_total"] == 1

    restarted = Worker(session_factory=session_factory, poll_interval_seconds=0.01, lease_seconds=5)
    restarted.request_shutdown()  # sweep at startup, then exit the loop immediately
    thread = threading.Thread(target=restarted.run_forever)
    thread.start()
    thread.join(timeout=20)
    assert not thread.is_alive()

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["engineer"] == WorkflowNodeRunStatus.RUNNING
    assert (state["agent_runs_total"], state["jobs_total"]) == (2, 2)


def test_worker_reconcile_failure_never_stops_the_worker(session_factory, monkeypatch):
    def boom(self):
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(WorkflowExecutionService, "reconcile_active_runs", boom)
    assert Worker(session_factory=session_factory).reconcile_workflows() == 0


def _run_lifespan(monkeypatch, engine, session_factory, *, schema_up_to_date):
    import app.main as main_module

    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(main_module, "SessionLocal", session_factory)
    monkeypatch.setattr(main_module, "is_schema_up_to_date", lambda _engine: schema_up_to_date)
    monkeypatch.setattr(main_module, "configure_logging", lambda: None)
    monkeypatch.setattr(main_module.settings, "backup_on_startup", False)

    async def go():
        async with main_module.lifespan(main_module.app):
            pass

    asyncio.run(go())


def test_fastapi_startup_runs_the_recovery_sweep(
    db, engine, session_factory, auth_token, bootstrap, worker, calls, monkeypatch
):
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)
    decide_then_die(session_factory, bootstrap, approval, monkeypatch)
    assert snapshot(session_factory, run.id)["agent_runs_total"] == 1

    _run_lifespan(monkeypatch, engine, session_factory, schema_up_to_date=True)

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["engineer"] == WorkflowNodeRunStatus.RUNNING
    assert (state["agent_runs_total"], state["jobs_total"]) == (2, 2)


def test_fastapi_startup_skips_the_sweep_on_a_stale_schema(engine, session_factory, auth_token, monkeypatch):
    calls_made = []
    monkeypatch.setattr(
        WorkflowExecutionService, "reconcile_active_runs", lambda self: calls_made.append(1) or 0
    )
    _run_lifespan(monkeypatch, engine, session_factory, schema_up_to_date=False)
    assert calls_made == []


def test_a_failing_startup_sweep_does_not_block_fastapi_startup(
    engine, session_factory, auth_token, bootstrap, monkeypatch
):
    def boom(self):
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(WorkflowExecutionService, "reconcile_active_runs", boom)
    _run_lifespan(monkeypatch, engine, session_factory, schema_up_to_date=True)  # must not raise


# --- authorization / fingerprint on workflow approvals -----------------------


def test_unauthenticated_and_cross_project_and_viewer_cannot_decide_a_workflow_approval(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    foreign = Project(org_id=bootstrap.organization.id, name="foreign")
    viewer_project = Project(org_id=bootstrap.organization.id, name="viewer-only")
    db.add_all([foreign, viewer_project])
    db.flush()
    db.add(
        ProjectMembership(project_id=viewer_project.id, user_id=bootstrap.user.id, role=ProjectRole.VIEWER)
    )
    db.commit()

    approvals_by_label = {}
    for label, project in (("foreign", foreign), ("viewer", viewer_project)):
        _built, run = waiting_run(db, session_factory, bootstrap, worker, label=label, project=project)
        approval = the_approval(session_factory, run.id)
        approvals_by_label[label] = approval

        assert (
            client.post(
                f"/approvals/{approval.id}/resolve",
                json={"approve": True, "action_fingerprint": approval.action_fingerprint},
            ).status_code
            == 401
        )  # no token
        assert resolve_via_api(client, auth_headers, approval, approve=True).status_code == 403
        assert resolve_via_api(client, auth_headers, approval, approve=False).status_code == 403

        state = snapshot(session_factory, run.id)  # nothing moved
        assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
        assert state["approvals"][0].status == ApprovalStatus.PENDING
        assert state["nodes"]["engineer"] == WorkflowNodeRunStatus.PENDING
        assert state["node_runs"]["engineer"].agent_run_id is None  # Engineer never dispatched

    assert audit_types(session_factory) == []
    # cross-project: not even readable; a project VIEWER can read what it cannot decide
    assert (
        client.get(f"/approvals/{approvals_by_label['foreign'].id}", headers=auth_headers).status_code == 403
    )
    assert (
        client.get(f"/approvals/{approvals_by_label['viewer'].id}", headers=auth_headers).status_code == 200
    )


def test_stale_fingerprint_is_rejected_and_the_workflow_keeps_waiting(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)

    response = resolve_via_api(client, auth_headers, approval, fingerprint="0" * 64)
    assert response.status_code == 409 and response.json()["error"]["code"] == "fingerprint_mismatch"

    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert state["approvals"][0].status == ApprovalStatus.PENDING
    assert worker.run_once() is False and audit_types(session_factory) == []


def test_upstream_artifact_changed_after_the_gate_was_requested_blocks_approval_atomically(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    """The second fingerprint check (at the gated transition): the approval
    can never advance the workflow if what it was bound to no longer matches."""
    _built, run = waiting_run(db, session_factory, bootstrap, worker)
    approval = the_approval(session_factory, run.id)
    artifact_id = approval.bound_artifact_id
    original_hash = db.execute(
        text("SELECT content_hash FROM artifacts WHERE id = :id"), {"id": artifact_id}
    ).scalar_one()
    db.execute(
        text("UPDATE artifacts SET content_hash = :h WHERE id = :id"), {"h": "e" * 64, "id": artifact_id}
    )
    db.commit()

    response = resolve_via_api(client, auth_headers, approval)  # echoes the original, correct fingerprint
    assert response.status_code == 409 and response.json()["error"]["code"] == "fingerprint_mismatch"

    state = snapshot(session_factory, run.id)  # decision, node and run all rolled back together
    assert state["approvals"][0].status == ApprovalStatus.PENDING
    assert state["approvals"][0].resolved_by is None
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert audit_types(session_factory) == [] and worker.run_once() is False

    # restoring the recorded hash proves nothing was left half-applied: the very
    # same approval can now be decided normally and the workflow resumes
    db.execute(
        text("UPDATE artifacts SET content_hash = :h WHERE id = :id"), {"h": original_hash, "id": artifact_id}
    )
    db.commit()
    assert resolve_via_api(client, auth_headers, approval).status_code == 200
    drain(worker)
    assert calls == ["planner", "engineer"]
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED


# --- validation ---------------------------------------------------------------


def _draft_with_gate(db, project, *, gate_config, gate_kwargs=None, extra_edge_into_gate=False):
    definitions = WorkflowDefinitionService(db)
    workflow = definitions.create_workflow(project.id, "validation-workflow")
    version = definitions.get_latest_version(workflow.id)
    from tests.conftest import make_agent, make_runnable_agent_version

    a = definitions.add_node(
        workflow.id,
        version.version,
        "a",
        WorkflowNodeType.AGENT,
        config={"agent_version_id": make_runnable_agent_version(db, make_agent(db, project, "va")).id},
    )
    gate = definitions.add_node(
        workflow.id,
        version.version,
        "gate",
        WorkflowNodeType.HUMAN_APPROVAL,
        config=gate_config,
        **(gate_kwargs or {}),
    )
    end = definitions.add_node(workflow.id, version.version, "end", WorkflowNodeType.TERMINAL)
    definitions.add_edge(workflow.id, version.version, a.id, gate.id)
    definitions.add_edge(workflow.id, version.version, gate.id, end.id)
    if extra_edge_into_gate:
        b = definitions.add_node(
            workflow.id,
            version.version,
            "b",
            WorkflowNodeType.AGENT,
            config={"agent_version_id": make_runnable_agent_version(db, make_agent(db, project, "vb")).id},
        )
        definitions.add_edge(workflow.id, version.version, b.id, gate.id)
    return definitions, workflow, version


def test_a_valid_gate_publishes(db, bootstrap):
    definitions, workflow, version = _draft_with_gate(
        db, bootstrap.project, gate_config={"approval_group": "leads"}
    )
    assert definitions.publish_version(workflow.id, version.version).status.value == "active"


@pytest.mark.parametrize(
    "gate_config,gate_kwargs,fragment",
    [
        ({}, None, "approval_group"),
        ({"approval_group": "   "}, None, "non-empty string"),
        ({"approval_group": 7}, None, "non-empty string"),
        ({"approval_group": "g", "auto_approve": True}, None, "auto_approve"),
        ({"approval_group": "g", "timeout_seconds": 60}, None, "timeout_seconds"),
        ({"approval_group": "g", "default_decision": "approve"}, None, "default_decision"),
        ({"approval_group": "g", "agent_version_id": "x"}, None, "agent_version_id"),
        ({"approval_group": "g", "approver_agent_version_id": "x"}, None, "approver_agent_version_id"),
        ({"approval_group": "g"}, {"timeout_seconds": 30}, "timeout_seconds"),
    ],
)
def test_gate_configuration_that_could_auto_approve_is_rejected_at_publish(
    db, bootstrap, gate_config, gate_kwargs, fragment
):
    definitions, workflow, version = _draft_with_gate(
        db, bootstrap.project, gate_config=gate_config, gate_kwargs=gate_kwargs
    )
    with pytest.raises(DAGValidationError) as exc:
        definitions.publish_version(workflow.id, version.version)
    assert any(fragment in issue for issue in exc.value.issues), exc.value.issues


def test_a_gate_with_two_upstream_dependencies_now_publishes(db, bootstrap):
    """MA7.3 rejected this ("exactly one is supported ... MA7.4"); MA7.4c makes
    the gate itself the ALL-of barrier. Everything else about a gate is still
    validated (see the forbidden-config tests above)."""
    definitions, workflow, version = _draft_with_gate(
        db, bootstrap.project, gate_config={"approval_group": "g"}, extra_edge_into_gate=True
    )
    assert definitions.publish_version(workflow.id, version.version).status.value == "active"


def test_a_multi_input_gate_waits_for_all_its_sources_and_runs_nothing(
    db, session_factory, bootstrap, worker, calls
):
    """MA7.4c (was: "runtime fails closed on a fan-in gate"): a gate with two
    inputs is dispatched only once BOTH are COMPLETED, becomes one durable wait
    with one Approval, and creates no AgentRun/TaskRun/job of its own."""
    definitions = WorkflowDefinitionService(db)
    workflow = definitions.create_workflow(bootstrap.project.id, "fanin")
    version = definitions.get_latest_version(workflow.id)
    from app.db.enums import TaskRunStatus, VersionStatus
    from app.models.tasks import TaskRun
    from tests.conftest import make_agent, make_runnable_agent_version, make_task

    def agent(key):
        return definitions.add_node(
            workflow.id,
            version.version,
            key,
            WorkflowNodeType.AGENT,
            config={"agent_version_id": make_runnable_agent_version(db, make_agent(db, bootstrap.project, key)).id},
        )

    a, b = agent("a"), agent("b")
    gate = definitions.add_node(
        workflow.id, version.version, "gate", WorkflowNodeType.HUMAN_APPROVAL, config={"approval_group": "g"}
    )
    end = definitions.add_node(workflow.id, version.version, "end", WorkflowNodeType.TERMINAL)
    for up, down in ((a, b), (b, gate), (a, gate), (gate, end)):
        definitions.add_edge(workflow.id, version.version, up.id, down.id)
    version.status = VersionStatus.ACTIVE
    task_run = TaskRun(task_id=make_task(db, bootstrap.project).id, status=TaskRunStatus.CREATED)
    db.add(task_run)
    db.commit()

    run = WorkflowExecutionService(db).start_workflow_run(version.id, task_run.id)
    drain(worker)

    state = snapshot(session_factory, run.id)
    assert calls == ["a", "b"]
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL  # after BOTH a and b
    assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert len(state["approvals"]) == 1
    assert "workflow.error" not in event_types(session_factory, task_run.id)
    assert (state["agent_runs_total"], state["jobs_total"]) == (2, 2)  # the gate created neither


def test_other_unsupported_node_types_still_fail_closed(db, session_factory, bootstrap, worker, calls):
    """MA7.2 behavior is preserved for everything that is not AGENT / TERMINAL /
    HUMAN_APPROVAL."""
    definitions = WorkflowDefinitionService(db)
    workflow = definitions.create_workflow(bootstrap.project.id, "unsupported")
    version = definitions.get_latest_version(workflow.id)
    from app.db.enums import TaskRunStatus, VersionStatus
    from app.models.tasks import TaskRun
    from tests.conftest import make_task

    definitions.add_node(workflow.id, version.version, "j", WorkflowNodeType.JUDGE, config={})
    version.status = VersionStatus.ACTIVE
    task_run = TaskRun(task_id=make_task(db, bootstrap.project).id, status=TaskRunStatus.CREATED)
    db.add(task_run)
    db.commit()
    run = WorkflowExecutionService(db).start_workflow_run(version.id, task_run.id)
    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.FAILED and state["nodes"]["j"] == WorkflowNodeRunStatus.FAILED
    assert state["approvals"] == []

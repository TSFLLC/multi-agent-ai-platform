"""MA7.4a -- concurrency-safety foundations for the MA7 workflow engine.

A. Flight Recorder isolation: durable workflow state first, evidence second.
B. Fresh-state correctness: decisions read current committed state.
C. Finalize-on-drain: a CANCELLING run converges to CANCELLED by itself.
D. Periodic idle reconciliation in the Worker.
E. Canonical upstream evidence: node_key order, source labels, de-duplication.
F. Conditional edges are rejected at publish (and refused at start).
G. Sequential MA7.2 / MA7.3 behavior is unchanged. (Fan-out was still NOT
   enabled when this file was written; MA7.4b enabled it -- see G below and
   tests/test_ma7_4b_parallel_execution.py.)

Parallel fan-out was deliberately not enabled in MA7.4a, so multi-parent graphs
are real published definitions whose node runs are put into chosen states by
hand (tests/ma7_4a_support.py) -- states the parallel engine now also produces
by itself. Every database is a disposable temp file.
"""

import itertools
import logging
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text

import app.services.execution_service as execution_service_module
import app.worker as worker_module
from app.config import Settings, settings
from app.db.enums import (
    AgentRunStatus,
    ApprovalStatus,
    VersionStatus,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
    WorkflowRunStatus,
)
from app.models.artifacts_eval import Artifact
from app.models.observability import ExecutionEvent
from app.models.tasks import AgentRun
from app.models.workflow import WorkflowNode, WorkflowNodeRun, WorkflowRun
from app.providers.base import InvokeResponse
from app.repositories.execution_event_repository import ExecutionEventRepository
from app.services.approval_service import WORKFLOW_HUMAN_APPROVAL_OPERATION, compute_action_fingerprint
from app.services.execution_service import AgentExecutionService, _MissingWorkflowContext
from app.services.flight_recorder import FlightRecorderService
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.workflow_execution_service import WorkflowExecutionError, WorkflowExecutionService
from app.services.workflow_validation_service import DAGValidationError
from app.worker import Worker
from tests.conftest import make_agent, make_agent_version, make_model, make_provider, make_provider_model
from tests.ma7_3b_support import (
    AGENT,
    APPROVAL,
    TERMINAL,
    build_chain,
    drain,
    event_types,
    install_fake_agents,
    resolve_via_api,
    snapshot,
    start,
)
from tests.ma7_4a_support import (
    BRANCHES,
    build_diamond,
    complete_node,
    finish_agent,
    manual_run,
    run_node,
    single_unsupported_node,
)
from tests.test_execution_service import FakeAdapter, _factory_for, _manual

PLANNER_FIRST = [("planner", AGENT), ("gate", APPROVAL), ("engineer", AGENT), ("result", TERMINAL)]
SEQUENTIAL = [("only", AGENT), ("result", TERMINAL)]


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
        reconcile_interval_seconds=0,
    )


def event_rows(session_factory, task_run_id):
    """(event_type, decision_summary, workflow_node_run_id, sequence_number) in order."""
    session = session_factory()
    try:
        return [
            (e.event_type, e.decision_summary, e.workflow_node_run_id, e.sequence_number)
            for e in session.execute(
                select(ExecutionEvent)
                .where(ExecutionEvent.task_run_id == task_run_id)
                .order_by(ExecutionEvent.sequence_number)
            )
            .scalars()
            .all()
        ]
    finally:
        session.close()


def status_of(session_factory, node_run_id):
    session = session_factory()
    try:
        return session.get(WorkflowNodeRun, node_run_id).status
    finally:
        session.close()


def run_status_of(session_factory, run_id):
    session = session_factory()
    try:
        return session.get(WorkflowRun, run_id).status
    finally:
        session.close()


def wait_until(predicate, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def keys_of(session, node_runs):
    return [session.get(WorkflowNode, nr.workflow_node_id).node_key for nr in node_runs]


# =============================================================================
# A. Flight Recorder isolation
# =============================================================================


def test_sequence_collision_cannot_roll_back_workflow_state(
    db, session_factory, bootstrap, calls, monkeypatch
):
    """The MA7.4 investigation's demonstrated hazard: the recorder loses a
    sequence-number race, rolls its session back to retry, and -- on the
    caller's session -- discards the caller's pending state. The node must be
    COMPLETED in the database and the event recorded exactly once."""
    built = build_chain(db, bootstrap.project, SEQUENTIAL, label="a1")
    run = start(db, built)  # events 1..N already exist for the parent task run
    node_run_id = snapshot(session_factory, run.id)["node_runs"]["only"].id
    parent = built.task_run.id
    assert len(event_rows(session_factory, parent)) >= 2

    # Injected at the repository, so the collision hits WHICHEVER session the
    # engine hands it (the caller's, if isolation ever regressed).
    stale = {"used": False}
    real_record = ExecutionEventRepository.record

    def colliding_record(self, session, **kwargs):
        real_execute = session.execute

        def execute(stmt, *args, **kw):
            result = real_execute(stmt, *args, **kw)
            if not stale["used"] and "max(" in str(stmt).lower():
                stale["used"] = True  # what a concurrent writer's stale read looks like
                return types.SimpleNamespace(scalar_one=lambda: 0)
            return result

        session.execute = execute
        try:
            return real_record(self, session, **kwargs)
        finally:
            del session.execute

    monkeypatch.setattr(ExecutionEventRepository, "record", colliding_record)

    caller = session_factory()
    try:
        node_run = caller.get(WorkflowNodeRun, node_run_id)
        node_run.status = WorkflowNodeRunStatus.COMPLETED  # pending, not yet committed
        WorkflowExecutionService(caller)._emit_event(
            parent, "workflow.node.completed", workflow_run_id=run.id, workflow_node_run_id=node_run_id
        )
    finally:
        caller.close()

    assert stale["used"] is True  # the collision really happened
    assert status_of(session_factory, node_run_id) == WorkflowNodeRunStatus.COMPLETED  # state survived
    rows = event_rows(session_factory, parent)
    assert [r[0] for r in rows].count("workflow.node.completed") == 1  # ...and so did the evidence
    sequences = [r[3] for r in rows]
    assert sequences == sorted(set(sequences))  # unique, ordered


def test_pending_state_is_durable_before_any_evidence_is_written(
    db, session_factory, bootstrap, calls, monkeypatch
):
    built = build_chain(db, bootstrap.project, SEQUENTIAL, label="a3")
    run = start(db, built)
    node_run_id = snapshot(session_factory, run.id)["node_runs"]["only"].id
    seen = {}
    real_record = FlightRecorderService.record

    def spy(self, **kwargs):
        seen["status"] = status_of(session_factory, node_run_id)  # what a different connection can see
        return real_record(self, **kwargs)

    monkeypatch.setattr(FlightRecorderService, "record", spy)
    caller = session_factory()
    try:
        caller.get(WorkflowNodeRun, node_run_id).status = WorkflowNodeRunStatus.COMPLETED
        WorkflowExecutionService(caller)._emit_event(
            built.task_run.id, "workflow.node.completed", workflow_node_run_id=node_run_id
        )
    finally:
        caller.close()
    assert seen["status"] == WorkflowNodeRunStatus.COMPLETED


def test_evidence_is_written_on_its_own_session_never_the_callers(
    db, session_factory, bootstrap, calls, monkeypatch
):
    built = build_chain(db, bootstrap.project, SEQUENTIAL, label="a4")
    start(db, built)
    captured = {}
    real_record = ExecutionEventRepository.record

    def spy(self, session, **kwargs):
        captured["session"] = session
        return real_record(self, session, **kwargs)

    monkeypatch.setattr(ExecutionEventRepository, "record", spy)
    caller = session_factory()
    try:
        WorkflowExecutionService(caller)._emit_event(built.task_run.id, "workflow.probe")
        assert captured["session"] is not caller
    finally:
        caller.close()


@contextmanager
def capture_engine_warnings():
    """Captures the engine module's own logger directly. ``caplog`` is not
    reliable here: alembic/env.py's ``fileConfig(...)`` (run by the migration
    tests earlier in the same process) disables every logger that already
    exists, so a root-level capture can see nothing in a full-suite run."""
    logger = logging.getLogger("app.services.workflow_execution_service")
    messages = []

    class Collect(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = Collect(level=logging.WARNING)
    was_disabled, old_level = logger.disabled, logger.level
    logger.disabled = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.disabled = was_disabled


@pytest.mark.parametrize("failure", ["exhausted_retries", "locked_database", "unexpected"])
def test_event_write_failure_is_swallowed_and_state_is_untouched(
    db, session_factory, bootstrap, calls, monkeypatch, failure
):
    built = build_chain(db, bootstrap.project, SEQUENTIAL, label=f"a2-{failure}")
    run = start(db, built)
    node_run_id = snapshot(session_factory, run.id)["node_runs"]["only"].id
    errors = {
        "exhausted_retries": RuntimeError("Could not assign a unique sequence_number after 5 attempts"),
        "locked_database": RuntimeError("database is locked"),
        "unexpected": ValueError("anything"),
    }

    def broken(self, session, **kwargs):
        raise errors[failure]

    monkeypatch.setattr(ExecutionEventRepository, "record", broken)
    caller = session_factory()
    try:
        caller.get(WorkflowNodeRun, node_run_id).status = WorkflowNodeRunStatus.COMPLETED
        with capture_engine_warnings() as warnings:
            WorkflowExecutionService(caller)._emit_event(built.task_run.id, "workflow.node.completed")
    finally:
        caller.close()

    assert status_of(session_factory, node_run_id) == WorkflowNodeRunStatus.COMPLETED
    assert any("workflow_event_not_recorded" in message for message in warnings)  # logged, not raised


@pytest.mark.parametrize("scenario", ["complete", "reject", "cancel_waiting"])
def test_a_workflow_makes_full_progress_even_if_every_event_write_fails(
    client, db, session_factory, auth_headers, bootstrap, worker, calls, monkeypatch, scenario
):
    """Evidence is observability only: with the recorder completely broken the
    workflow (including an approval gate and a cancellation) still reaches
    exactly the right terminal state."""

    def broken(self, session, **kwargs):
        raise RuntimeError("recorder down")

    monkeypatch.setattr(ExecutionEventRepository, "record", broken)
    built = build_chain(db, bootstrap.project, PLANNER_FIRST, label=f"a2f-{scenario}")
    run = start(db, built)
    drain(worker)
    approval = snapshot(session_factory, run.id)["approvals"][0]

    if scenario == "complete":
        assert resolve_via_api(client, auth_headers, approval).status_code == 200
        drain(worker)
        assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED
        assert calls == ["planner", "engineer"]
    elif scenario == "reject":
        assert resolve_via_api(client, auth_headers, approval, approve=False).status_code == 200
        assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.FAILED
    else:
        assert client.post(f"/workflow-runs/{run.id}/cancel", headers=auth_headers).status_code == 200
        state = snapshot(session_factory, run.id)
        assert state["run"] == WorkflowRunStatus.CANCELLED
        assert state["approvals"][0].status == ApprovalStatus.EXPIRED
    assert event_rows(session_factory, built.task_run.id) == []  # and indeed nothing was recorded


def test_concurrent_event_writers_never_lose_state(db, session_factory, bootstrap, calls):
    """Real sequence races: four 'completing branches' write engine events to
    the same parent task run at once, each with a pending state change."""
    built = build_diamond(db, bootstrap.project, label="a5")
    run, node_runs = manual_run(db, built)
    keys = ["eng", "test", "security", "code"]
    barrier = threading.Barrier(len(keys))

    def complete_and_emit(key):
        session = session_factory()
        try:
            session.get(WorkflowNodeRun, node_runs[key].id).status = WorkflowNodeRunStatus.COMPLETED
            barrier.wait(timeout=10)
            WorkflowExecutionService(session)._emit_event(
                built.task_run.id,
                "workflow.node.completed",
                workflow_run_id=run.id,
                workflow_node_run_id=node_runs[key].id,
            )
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        list(pool.map(complete_and_emit, keys))

    assert all(status_of(session_factory, node_runs[k].id) == WorkflowNodeRunStatus.COMPLETED for k in keys)
    sequences = [r[3] for r in event_rows(session_factory, built.task_run.id)]
    assert sequences == sorted(
        set(sequences)
    )  # unique; a swallowed retry-exhaustion may drop an event, never state


def _install_state_spy(monkeypatch, session_factory):
    """At the instant ANY workflow event is recorded, check that the state it
    describes is already durable -- one invariant over every emit call site."""
    observations = []
    real_record = FlightRecorderService.record

    def spy(self, **kwargs):
        session = session_factory()
        try:
            node_status = None
            if kwargs.get("workflow_node_run_id"):
                node_status = session.get(WorkflowNodeRun, kwargs["workflow_node_run_id"]).status
            run_status = None
            if kwargs.get("workflow_run_id"):
                run_status = session.get(WorkflowRun, kwargs["workflow_run_id"]).status
        finally:
            session.close()
        observations.append((kwargs["event_type"], kwargs.get("decision_summary"), node_status, run_status))
        return real_record(self, **kwargs)

    monkeypatch.setattr(FlightRecorderService, "record", spy)
    return observations


NODE_EXPECT = {
    "workflow.node.completed": {WorkflowNodeRunStatus.COMPLETED},
    "workflow.node.failed": {WorkflowNodeRunStatus.FAILED},
    "workflow.node.cancelled": {WorkflowNodeRunStatus.CANCELLED},
    "workflow.node.waiting_for_approval": {WorkflowNodeRunStatus.WAITING_FOR_APPROVAL},
    "approval.requested": {WorkflowNodeRunStatus.WAITING_FOR_APPROVAL},
    "approval.approved": {WorkflowNodeRunStatus.COMPLETED},
    "approval.rejected": {WorkflowNodeRunStatus.FAILED},
    "approval.cancelled": {WorkflowNodeRunStatus.CANCELLED},
}
RUN_EXPECT = {
    "workflow.started": {WorkflowRunStatus.RUNNING},
    "workflow.completed": {WorkflowRunStatus.COMPLETED},
    "workflow.failed": {WorkflowRunStatus.FAILED},
    "workflow.error": {WorkflowRunStatus.FAILED},
}


def test_every_workflow_event_describes_state_that_is_already_durable(
    client, db, session_factory, auth_headers, bootstrap, worker, calls, monkeypatch
):
    """Audit of all emit call sites: happy path with an approval gate, reject,
    cancel-while-waiting, cancel with an in-flight agent, (MA7.4b) a parallel
    fan-out that completes, a parallel branch failure that drains its
    siblings, and a node that cannot be dispatched at all."""
    observations = _install_state_spy(monkeypatch, session_factory)

    # happy path + approval gate
    built, run = _waiting(db, session_factory, bootstrap, worker, "spy-ok")
    approval = snapshot(session_factory, run.id)["approvals"][0]
    resolve_via_api(client, auth_headers, approval)
    drain(worker)
    # reject
    built, run = _waiting(db, session_factory, bootstrap, worker, "spy-rej")
    resolve_via_api(client, auth_headers, snapshot(session_factory, run.id)["approvals"][0], approve=False)
    # cancel while waiting
    built, run = _waiting(db, session_factory, bootstrap, worker, "spy-can")
    client.post(f"/workflow-runs/{run.id}/cancel", headers=auth_headers)
    # cancel with an in-flight agent, then it stops
    built = build_chain(db, bootstrap.project, SEQUENTIAL, label="spy-fly")
    run = start(db, built)
    WorkflowExecutionService(db).cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)
    agent = db.get(AgentRun, snapshot(session_factory, run.id)["node_runs"]["only"].agent_run_id)
    finish_agent(db, agent, AgentRunStatus.STOPPED)
    WorkflowExecutionService(db).on_agent_run_complete(agent.id)
    # MA7.4b: a parallel fan-out/fan-in that completes...
    diamond = build_diamond(db, bootstrap.project, label="spy-multi")
    drun = WorkflowExecutionService(db).start_workflow_run(diamond.published.id, diamond.task_run.id)
    drain(worker)
    assert snapshot(session_factory, drun.id)["run"] == WorkflowRunStatus.COMPLETED
    # ...one whose branch fails while its siblings are still in flight (fail fast, drain, finalize)
    failing = build_diamond(db, bootstrap.project, label="spy-par-fail")
    frun = WorkflowExecutionService(db).start_workflow_run(failing.published.id, failing.task_run.id)
    eng_agent = db.get(AgentRun, snapshot(session_factory, frun.id)["node_runs"]["eng"].agent_run_id)
    finish_agent(db, eng_agent, AgentRunStatus.COMPLETED)
    WorkflowExecutionService(db).on_agent_run_complete(eng_agent.id)  # fans out to test/security/code
    branches = snapshot(session_factory, frun.id)["node_runs"]
    security_agent = db.get(AgentRun, branches["security"].agent_run_id)
    finish_agent(db, security_agent, AgentRunStatus.FAILED)
    WorkflowExecutionService(db).on_agent_run_complete(security_agent.id)
    for key in ("test", "code"):  # the siblings stop cooperatively
        sibling = db.get(AgentRun, branches[key].agent_run_id)
        finish_agent(db, sibling, AgentRunStatus.STOPPED)
        WorkflowExecutionService(db).on_agent_run_complete(sibling.id)
    assert snapshot(session_factory, frun.id)["run"] == WorkflowRunStatus.FAILED
    # ...and a node that cannot be dispatched at all (fail closed, sequential)
    version_id, task_run_id = single_unsupported_node(db, bootstrap.project, "spy-judge")
    WorkflowExecutionService(db).start_workflow_run(version_id, task_run_id)

    seen_types = {o[0] for o in observations}
    for expected in (
        "workflow.node.completed", "approval.requested", "approval.approved", "approval.rejected",
        "approval.cancelled", "workflow.node.cancelled", "workflow.completed", "workflow.failed",
        "workflow.cancelled", "workflow.error",
    ):  # fmt: skip
        assert expected in seen_types, expected

    violations = []
    for event_type, summary, node_status, run_status in observations:
        if event_type in NODE_EXPECT and node_status not in NODE_EXPECT[event_type]:
            violations.append((event_type, "node", node_status))
        if event_type in RUN_EXPECT and run_status not in RUN_EXPECT[event_type]:
            violations.append((event_type, "run", run_status))
        if event_type == "workflow.cancelled":
            expected = {"cancellation requested": {WorkflowRunStatus.CANCELLING, WorkflowRunStatus.CANCELLED}}
            allowed = expected.get(summary, {WorkflowRunStatus.CANCELLED})  # "cancellation complete"
            if run_status not in allowed:
                violations.append((event_type, summary, run_status))
    assert violations == []


def _waiting(db, session_factory, bootstrap, worker, label):
    built = build_chain(db, bootstrap.project, PLANNER_FIRST, label=label)
    run = start(db, built)
    drain(worker)
    return built, run


# =============================================================================
# B. Fresh-state correctness
# =============================================================================


def test_find_ready_nodes_sees_a_sibling_completed_by_another_session(
    db, session_factory, bootstrap, tmp_path
):
    built = build_diamond(db, bootstrap.project, label="b1")
    run, node_runs = manual_run(db, built)
    for key in ("eng", "test", "security"):
        complete_node(db, built, node_runs, key)
    run_node(db, built, node_runs, "code")  # the last branch is still in flight

    stale_session = session_factory()
    try:
        service = WorkflowExecutionService(stale_session)
        assert (
            keys_of(stale_session, service._find_ready_nodes(run.id)) == []
        )  # join not ready; warms the session
        # another process completes the last branch and commits
        db.execute(
            text("UPDATE workflow_node_runs SET status='completed' WHERE id=:id"),
            {"id": node_runs["code"].id},
        )
        db.commit()
        # same session, no expiry: must still see it
        assert keys_of(stale_session, service._find_ready_nodes(run.id)) == ["join"]
    finally:
        stale_session.close()


def test_check_workflow_complete_sees_the_last_node_finished_by_another_session(
    db, session_factory, bootstrap
):
    built = build_diamond(db, bootstrap.project, label="b2")
    run, node_runs = manual_run(db, built)
    for key in node_runs:
        if key != "end":
            complete_node(db, built, node_runs, key)
    run_node(db, built, node_runs, "end")

    stale_session = session_factory()
    try:
        service = WorkflowExecutionService(stale_session)
        service._check_workflow_complete(run.id)  # not all terminal: no-op, warms the session
        assert run_status_of(session_factory, run.id) == WorkflowRunStatus.RUNNING
        db.execute(
            text("UPDATE workflow_node_runs SET status='completed' WHERE id=:id"), {"id": node_runs["end"].id}
        )
        db.commit()
        service._check_workflow_complete(run.id)
    finally:
        stale_session.close()
    assert run_status_of(session_factory, run.id) == WorkflowRunStatus.COMPLETED


def test_upstream_collection_sees_a_source_completed_by_another_session(
    db, session_factory, bootstrap, tmp_path
):
    built = build_diamond(db, bootstrap.project, label="b3")
    _run, node_runs = manual_run(db, built)
    test_artifact = complete_node(db, built, node_runs, "test", text="T", tmp_path=tmp_path)
    run_node(db, built, node_runs, "security")

    stale_session = session_factory()
    try:
        service = WorkflowExecutionService(stale_session)
        join = stale_session.get(WorkflowNodeRun, node_runs["join"].id)
        assert service._collect_upstream_artifacts(join) == [test_artifact]
        # This session already holds the in-flight 'security' node run (RUNNING,
        # no output) -- exactly the stale identity-mapped row a plain query
        # would hand back after another process completes it.
        held = stale_session.get(WorkflowNodeRun, node_runs["security"].id)
        assert held.status == WorkflowNodeRunStatus.RUNNING and held.output_snapshot_ref is None
        security_artifact = complete_node(db, built, node_runs, "security", text="S", tmp_path=tmp_path)
        assert service._collect_upstream_artifacts(join) == [
            security_artifact,
            test_artifact,
        ]  # security < test
    finally:
        stale_session.close()


def test_cancelling_pending_nodes_never_overwrites_a_node_a_dispatcher_just_claimed(
    db, session_factory, bootstrap, monkeypatch
):
    built = build_diamond(db, bootstrap.project, label="b4")
    run, node_runs = manual_run(db, built)
    real_cas = WorkflowExecutionService._cas_node_run
    raced = {"done": False}

    def racing_cas(self, node_run_id, expected, **values):
        if not raced["done"]:
            # The window is between the cancel's SELECT and its FIRST write (after
            # that, its own UPDATEs hold SQLite's write lock): a dispatcher claims
            # 'join' right there.
            raced["done"] = True
            other = session_factory()
            other.execute(
                text("UPDATE workflow_node_runs SET status='running' WHERE id=:id"),
                {"id": node_runs["join"].id},
            )
            other.commit()
            other.close()
        return real_cas(self, node_run_id, expected, **values)

    monkeypatch.setattr(WorkflowExecutionService, "_cas_node_run", racing_cas)
    WorkflowExecutionService(db)._cancel_pending_nodes(run.id)

    assert raced["done"]
    assert status_of(session_factory, node_runs["join"].id) == WorkflowNodeRunStatus.RUNNING  # left alone
    for key in ("eng", "test", "security", "code", "end"):
        assert status_of(session_factory, node_runs[key].id) == WorkflowNodeRunStatus.CANCELLED


def test_concurrent_completion_replays_record_the_outcome_and_dispatch_once(
    db, session_factory, bootstrap, tmp_path, calls
):
    built = build_chain(
        db, bootstrap.project, [("first", AGENT), ("second", AGENT), ("result", TERMINAL)], label="b5"
    )
    run = start(db, built)
    first = snapshot(session_factory, run.id)["node_runs"]["first"]
    agent = db.get(AgentRun, first.agent_run_id)
    finish_agent(db, agent, AgentRunStatus.COMPLETED, text="OUT", tmp_path=tmp_path)
    barrier = threading.Barrier(4)

    def replay(_):
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            WorkflowExecutionService(session).on_agent_run_complete(agent.id)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(replay, range(4)))

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["first"] == WorkflowNodeRunStatus.COMPLETED
    assert state["nodes"]["second"] == WorkflowNodeRunStatus.RUNNING
    assert (state["agent_runs_total"], state["task_runs_total"], state["jobs_total"]) == (
        2,
        3,
        2,
    )  # one dispatch
    completed = [
        r for r in event_rows(session_factory, built.task_run.id) if r[0] == "workflow.node.completed"
    ]
    assert [r[2] for r in completed].count(first.id) == 1  # only the winning replay records it


# =============================================================================
# C. Finalize-on-drain
# =============================================================================


def _cancel_events(session_factory, task_run_id):
    return [r[1] for r in event_rows(session_factory, task_run_id) if r[0] == "workflow.cancelled"]


def test_cancelling_run_finalizes_by_itself_when_its_last_agent_stops(db, session_factory, bootstrap, calls):
    built = build_chain(db, bootstrap.project, SEQUENTIAL, label="c1")
    run = start(db, built)  # 'only' is in flight
    service = WorkflowExecutionService(db)
    service.cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)
    assert run_status_of(session_factory, run.id) == WorkflowRunStatus.CANCELLING  # waiting on the agent

    agent = db.get(AgentRun, snapshot(session_factory, run.id)["node_runs"]["only"].agent_run_id)
    assert agent.cancellation_requested_at is not None  # cooperative request was signalled
    finish_agent(db, agent, AgentRunStatus.STOPPED)
    WorkflowExecutionService(db).on_agent_run_complete(agent.id)  # the ONLY call -- no reconcile/sweep

    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.CANCELLED and state["run_ended_at"] is not None
    assert set(state["nodes"].values()) == {WorkflowNodeRunStatus.CANCELLED}
    assert _cancel_events(session_factory, built.task_run.id) == [
        "cancellation requested",
        "cancellation complete",
    ]


def test_drain_finalizes_only_after_the_last_in_flight_node_and_preserves_completed_siblings(
    db, session_factory, bootstrap, tmp_path
):
    built = build_diamond(db, bootstrap.project, label="c2")
    run, node_runs = manual_run(db, built)
    eng_artifact = complete_node(db, built, node_runs, "eng", text="ENGINEER", tmp_path=tmp_path)
    test_agent = run_node(db, built, node_runs, "test")
    security_agent = run_node(db, built, node_runs, "security")  # code, join, end still PENDING

    WorkflowExecutionService(db).cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)
    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.CANCELLING
    assert {k: state["nodes"][k] for k in ("code", "join", "end")} == {
        k: WorkflowNodeRunStatus.CANCELLED for k in ("code", "join", "end")
    }
    assert (state["nodes"]["test"], state["nodes"]["security"]) == (WorkflowNodeRunStatus.RUNNING,) * 2

    # first sibling finishes normally: its result is kept, the run is NOT finalized yet
    finish_agent(db, test_agent, AgentRunStatus.COMPLETED, text="TEST REPORT", tmp_path=tmp_path)
    WorkflowExecutionService(db).on_agent_run_complete(test_agent.id)
    mid = snapshot(session_factory, run.id)
    assert mid["run"] == WorkflowRunStatus.CANCELLING
    assert mid["nodes"]["test"] == WorkflowNodeRunStatus.COMPLETED
    assert mid["node_runs"]["test"].output_snapshot_ref is not None

    # the last in-flight node stops: the run converges to CANCELLED on its own
    finish_agent(db, security_agent, AgentRunStatus.STOPPED)
    WorkflowExecutionService(db).on_agent_run_complete(security_agent.id)
    final = snapshot(session_factory, run.id)
    assert final["run"] == WorkflowRunStatus.CANCELLED and final["run_ended_at"] is not None
    assert final["nodes"]["eng"] == WorkflowNodeRunStatus.COMPLETED  # completed work is never rewritten
    assert final["nodes"]["test"] == WorkflowNodeRunStatus.COMPLETED
    assert final["node_runs"]["eng"].output_snapshot_ref == eng_artifact
    assert final["nodes"]["security"] == WorkflowNodeRunStatus.CANCELLED


def test_finalization_is_idempotent_under_repetition_and_reconciliation(
    db, session_factory, bootstrap, tmp_path
):
    built = build_diamond(db, bootstrap.project, label="c3")
    run, node_runs = manual_run(db, built)
    complete_node(db, built, node_runs, "eng", text="E", tmp_path=tmp_path)
    agent = run_node(db, built, node_runs, "test")
    WorkflowExecutionService(db).cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)
    finish_agent(db, agent, AgentRunStatus.STOPPED)
    WorkflowExecutionService(db).on_agent_run_complete(agent.id)

    settled = snapshot(session_factory, run.id)
    events_before = event_rows(session_factory, built.task_run.id)
    assert settled["run"] == WorkflowRunStatus.CANCELLED

    for _ in range(3):
        service = WorkflowExecutionService(db)
        service.on_agent_run_complete(agent.id)  # replayed worker completion
        service._check_workflow_complete(run.id)
        service.reconcile_workflow_run(run.id)
        service.reconcile_active_runs()
        service.cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)  # late duplicate cancel

    after = snapshot(session_factory, run.id)
    assert after["run"] == WorkflowRunStatus.CANCELLED
    assert after["run_ended_at"] == settled["run_ended_at"]  # never re-finalized
    assert after["nodes"] == settled["nodes"]
    assert event_rows(session_factory, built.task_run.id) == events_before  # not one extra event
    assert _cancel_events(session_factory, built.task_run.id).count("cancellation complete") == 1


def test_concurrent_finalizers_make_exactly_one_transition(db, session_factory, bootstrap):
    built = build_diamond(db, bootstrap.project, label="c4")
    run, node_runs = manual_run(db, built, status=WorkflowRunStatus.CANCELLING)
    for node_run in node_runs.values():
        node_run.status = WorkflowNodeRunStatus.CANCELLED  # everything already drained
    db.commit()
    barrier = threading.Barrier(6)

    def finalize(_):
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            WorkflowExecutionService(session)._check_workflow_complete(run.id)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(finalize, range(6)))

    assert run_status_of(session_factory, run.id) == WorkflowRunStatus.CANCELLED
    assert _cancel_events(session_factory, built.task_run.id) == ["cancellation complete"]


def test_drain_through_the_real_worker_needs_no_sweep(db, session_factory, bootstrap, worker, monkeypatch):
    """Production path: cancel a run whose Planner job is queued; the Worker
    claims it, the agent stops cooperatively, and the run is CANCELLED -- with
    no reconciliation sweep anywhere."""

    def cooperative_stop(self, agent_run_id, *, worker_id):
        agent_run = self.db.get(AgentRun, agent_run_id)
        assert agent_run.cancellation_requested_at is not None
        agent_run.status = AgentRunStatus.STOPPED
        self.db.commit()

    monkeypatch.setattr(execution_service_module.AgentExecutionService, "execute", cooperative_stop)
    built = build_chain(db, bootstrap.project, PLANNER_FIRST, label="c5")
    run = start(db, built)
    WorkflowExecutionService(db).cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)
    assert run_status_of(session_factory, run.id) == WorkflowRunStatus.CANCELLING

    assert worker.run_once() is True
    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.CANCELLED and state["run_ended_at"] is not None
    assert worker.run_once() is False


def test_a_repeated_cancel_keeps_the_original_request_provenance(db, session_factory, bootstrap, calls):
    built = build_chain(db, bootstrap.project, SEQUENTIAL, label="c9")
    run = start(db, built)
    service = WorkflowExecutionService(db)
    service.cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)
    first = session_factory()
    try:
        first_row = first.get(WorkflowRun, run.id)
        requested = (first_row.cancellation_requested_at, first_row.cancellation_requested_by)
    finally:
        first.close()
    service.cancel_workflow_run(run.id, cancelled_by_user_id=None)  # a later cancel with no user

    check = session_factory()
    try:
        row = check.get(WorkflowRun, run.id)
        assert (row.cancellation_requested_at, row.cancellation_requested_by) == requested
        assert row.cancellation_requested_by == bootstrap.user.id
    finally:
        check.close()


# =============================================================================
# D. Periodic idle reconciliation
# =============================================================================


def test_reconcile_interval_logic_is_throttled_and_never_a_busy_loop(session_factory, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        worker_module, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep)
    )
    sweeps = []
    monkeypatch.setattr(
        Worker, "reconcile_workflows", lambda self, *, periodic=False: sweeps.append(periodic) or 0
    )
    w = Worker(session_factory=session_factory, reconcile_interval_seconds=30)

    for now, due in ((0.0, False), (29.9, False), (30.0, True), (30.1, False), (59.9, False), (60.0, True)):
        clock[0] = now
        assert (w._reconcile_if_due() is not None) is due, now
    assert sweeps == [True, True]  # only periodic sweeps; two in 60s, not one per idle poll

    disabled = Worker(session_factory=session_factory, reconcile_interval_seconds=0)
    clock[0] = 10_000.0
    assert disabled._reconcile_if_due() is None


def test_reconcile_interval_setting_is_bounded():
    assert Settings().worker_reconcile_interval_seconds == 60.0
    assert Settings(worker_reconcile_interval_seconds=0).worker_reconcile_interval_seconds == 0
    assert Settings(worker_reconcile_interval_seconds=1.0).worker_reconcile_interval_seconds == 1.0
    for too_small in (0.1, 0.99, -5):
        with pytest.raises(ValueError):
            Settings(worker_reconcile_interval_seconds=too_small)
    assert Worker().reconcile_interval_seconds == settings.worker_reconcile_interval_seconds


def _stalled_after_planner(db, session_factory, bootstrap, monkeypatch, label, interval):
    """A run whose gate dispatch fails transiently right after the Planner's
    completion commit (process alive; only that step failed)."""
    built = build_chain(db, bootstrap.project, PLANNER_FIRST, label=label)
    run = start(db, built)
    real_dispatch = WorkflowExecutionService._dispatch_human_approval
    monkeypatch.setattr(
        WorkflowExecutionService,
        "_dispatch_human_approval",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("transient failure")),
    )
    w = Worker(
        session_factory=session_factory,
        poll_interval_seconds=0.01,
        lease_seconds=5,
        reconcile_interval_seconds=interval,
    )
    thread = threading.Thread(target=w.run_forever)
    thread.start()
    return built, run, real_dispatch, w, thread


def test_idle_worker_recovers_a_stalled_run_without_a_restart(
    db, session_factory, bootstrap, calls, monkeypatch
):
    _built, run, real_dispatch, w, thread = _stalled_after_planner(
        db, session_factory, bootstrap, monkeypatch, "d3", interval=0.2
    )
    try:
        assert wait_until(
            lambda: snapshot(session_factory, run.id)["nodes"]["planner"] == WorkflowNodeRunStatus.COMPLETED
        )
        time.sleep(0.6)  # several intervals: the sweeps keep failing while the fault persists
        stalled = snapshot(session_factory, run.id)
        assert stalled["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and stalled["approvals"] == []

        monkeypatch.setattr(
            WorkflowExecutionService, "_dispatch_human_approval", real_dispatch
        )  # fault clears
        assert wait_until(
            lambda: snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
        )
    finally:
        w.request_shutdown()
        thread.join(timeout=20)

    assert not thread.is_alive()
    state = snapshot(session_factory, run.id)
    assert len(state["approvals"]) == 1 and state["approvals"][0].status == ApprovalStatus.PENDING
    assert (state["agent_runs_total"], state["jobs_total"]) == (
        1,
        1,
    )  # recovered without duplicating anything


def test_periodic_reconciliation_can_be_disabled(db, session_factory, bootstrap, calls, monkeypatch):
    _built, run, real_dispatch, w, thread = _stalled_after_planner(
        db, session_factory, bootstrap, monkeypatch, "d4", interval=0
    )
    try:
        assert wait_until(
            lambda: snapshot(session_factory, run.id)["nodes"]["planner"] == WorkflowNodeRunStatus.COMPLETED
        )
        monkeypatch.setattr(WorkflowExecutionService, "_dispatch_human_approval", real_dispatch)
        time.sleep(0.8)  # plenty of idle polls, no interval configured
        assert snapshot(session_factory, run.id)["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING
    finally:
        w.request_shutdown()
        thread.join(timeout=20)


def test_idle_loop_sweeps_only_at_startup_within_a_long_interval(session_factory, monkeypatch):
    counts = []
    monkeypatch.setattr(
        Worker, "reconcile_workflows", lambda self, *, periodic=False: counts.append(periodic) or 0
    )
    w = Worker(session_factory=session_factory, poll_interval_seconds=0.01, reconcile_interval_seconds=1000)
    thread = threading.Thread(target=w.run_forever)
    thread.start()
    time.sleep(0.5)  # ~50 idle polls
    w.request_shutdown()
    thread.join(timeout=20)
    assert counts == [False]  # the startup sweep only; the idle loop did not spin sweeps


def test_default_worker_still_processes_one_job_per_run_once(db, session_factory, bootstrap, calls):
    """Worker concurrency is NOT part of MA7.4a: single-job behavior is unchanged."""
    first = build_chain(db, bootstrap.project, SEQUENTIAL, label="d6a")
    second = build_chain(db, bootstrap.project, SEQUENTIAL, label="d6b")
    start(db, first)
    start(db, second)
    w = Worker(session_factory=session_factory, poll_interval_seconds=0.01, lease_seconds=5)
    assert w.run_once() is True
    assert calls == ["only"]  # exactly one node executed by that call
    assert w.run_once() is True and calls == ["only", "only"]


# =============================================================================
# E. Canonical upstream evidence
# =============================================================================


def _artifacts_by_branch(db, built, node_runs, tmp_path):
    complete_node(db, built, node_runs, "eng", text="ENGINEER", tmp_path=tmp_path)
    return {
        key: complete_node(db, built, node_runs, key, text=f"REPORT-{key}", tmp_path=tmp_path)
        for key in BRANCHES
    }


def _dispatch_join(db, node_runs):
    WorkflowExecutionService(db)._dispatch_node_for_execution(node_runs["join"])
    db.refresh(node_runs["join"])
    return db.get(AgentRun, node_runs["join"].agent_run_id)


@pytest.mark.parametrize("insert_order", list(itertools.permutations(BRANCHES)), ids="-".join)
def test_canonical_order_is_independent_of_edge_insertion_order(db, bootstrap, tmp_path, insert_order):
    built = build_diamond(
        db, bootstrap.project, insert_order=insert_order, label="e1-" + "-".join(insert_order)
    )
    _run, node_runs = manual_run(db, built)
    artifact = _artifacts_by_branch(db, built, node_runs, tmp_path)
    service = WorkflowExecutionService(db)
    expected_keys = ["code", "security", "test"]  # node_key ascending

    assert [k for k, _ in service._upstream_sources(node_runs["join"])] == expected_keys
    assert service._collect_upstream_artifacts(node_runs["join"]) == [artifact[k] for k in expected_keys]
    assert [nr.id for nr in service._get_upstream_node_runs(node_runs["join"])] == [
        node_runs[k].id for k in expected_keys
    ]

    context = _dispatch_join(db, node_runs).input_context_json
    assert context["kind"] == "workflow_upstream"
    assert context["upstream_artifact_ids"] == [artifact[k] for k in expected_keys]
    assert context["upstream_node_run_ids"] == [node_runs[k].id for k in expected_keys]
    assert [u["node_key"] for u in context["upstream"]] == expected_keys


def test_duplicate_artifact_ids_are_deduplicated_deterministically(db, bootstrap, tmp_path):
    built = build_diamond(db, bootstrap.project, insert_order=("test", "code", "security"), label="e2")
    _run, node_runs = manual_run(db, built)
    complete_node(db, built, node_runs, "eng", text="E", tmp_path=tmp_path)
    code = complete_node(db, built, node_runs, "code", text="C", tmp_path=tmp_path)
    shared = complete_node(db, built, node_runs, "security", text="SHARED", tmp_path=tmp_path)
    complete_node(db, built, node_runs, "test", artifact_id=shared)  # same artifact via a second source

    context = _dispatch_join(db, node_runs).input_context_json
    assert context["upstream_artifact_ids"] == [code, shared]  # once each, canonical order
    assert [(u["artifact_id"], u["node_key"], u["source_node_keys"]) for u in context["upstream"]] == [
        (code, "code", ["code"]),
        (shared, "security", ["security", "test"]),  # first source in canonical order; none dropped
    ]
    assert context["upstream_node_run_ids"] == [
        node_runs[k].id for k in ("code", "security", "test")
    ]  # runs: no dedupe


def test_source_node_labels_reach_the_prompt_in_canonical_order(db, bootstrap, tmp_path):
    built = build_diamond(db, bootstrap.project, insert_order=("test", "security", "code"), label="e3")
    _run, node_runs = manual_run(db, built)
    complete_node(db, built, node_runs, "eng", text="E", tmp_path=tmp_path)
    complete_node(db, built, node_runs, "code", text="CODE-REVIEW-TEXT", tmp_path=tmp_path)
    shared = complete_node(db, built, node_runs, "security", text="SECURITY-TEXT", tmp_path=tmp_path)
    complete_node(db, built, node_runs, "test", artifact_id=shared)

    prompt = AgentExecutionService(db)._build_extra_context(_dispatch_join(db, node_runs))

    assert (
        prompt.index("source_node: code")
        < prompt.index("CODE-REVIEW-TEXT")
        < prompt.index("source_node: security, test")
    )
    assert prompt.index("CODE-REVIEW-TEXT") < prompt.index("SECURITY-TEXT")
    assert prompt.count("SECURITY-TEXT") == 1  # the shared artifact is rendered once
    assert prompt.count("--- UPSTREAM WORKFLOW OUTPUT ---") == 2


def test_runs_dispatched_before_labels_existed_render_unlabelled(db, bootstrap, tmp_path):
    built = build_diamond(db, bootstrap.project, label="e6")
    _run, node_runs = manual_run(db, built)
    artifact = complete_node(db, built, node_runs, "test", text="LEGACY", tmp_path=tmp_path)
    agent = run_node(db, built, node_runs, "join")
    agent.input_context_json = {"kind": "workflow_upstream", "upstream_artifact_ids": [artifact]}
    db.commit()

    prompt = AgentExecutionService(db)._build_extra_context(agent)
    assert "LEGACY" in prompt and "source_node:" not in prompt


@pytest.mark.parametrize("damage", ["tamper_content", "delete_file", "delete_artifact_row"])
def test_missing_or_tampered_upstream_artifacts_still_fail_closed(db, bootstrap, tmp_path, damage):
    built = build_diamond(db, bootstrap.project, label=f"e4-{damage}")
    _run, node_runs = manual_run(db, built)
    artifact = _artifacts_by_branch(db, built, node_runs, tmp_path)
    agent = _dispatch_join(db, node_runs)
    service = AgentExecutionService(db)
    assert "REPORT-security" in service._build_extra_context(agent)  # intact: renders

    victim = db.get(Artifact, artifact["security"])  # damage the MIDDLE artifact of three
    if damage == "tamper_content":
        Path(victim.storage_ref).write_text("changed", encoding="utf-8")
        expected = "sha256"
    elif damage == "delete_file":
        Path(victim.storage_ref).unlink()
        expected = "could not be read"
    else:
        db.delete(victim)
        db.commit()
        expected = "no longer exists"
    with pytest.raises(_MissingWorkflowContext, match=expected):
        service._build_extra_context(agent)


def test_a_tampered_upstream_artifact_fails_the_run_before_any_provider_call(
    db, bootstrap, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "artifacts_dir", tmp_path / "out")
    built = build_diamond(db, bootstrap.project, label="e4x")
    _run, node_runs = manual_run(db, built)
    artifact = _artifacts_by_branch(db, built, node_runs, tmp_path)
    model = make_model(db, canonical_model_id="free/model")
    provider_model = make_provider_model(
        db,
        model=model,
        provider=make_provider(db),
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    join_version = built.agent_versions["join"]
    join_version.status = VersionStatus.ACTIVE
    join_version.model_policy = _manual(provider_model)
    db.commit()
    agent = _dispatch_join(db, node_runs)
    Path(db.get(Artifact, artifact["test"]).storage_ref).write_text(
        "altered after approval", encoding="utf-8"
    )
    adapter = FakeAdapter(
        responses=[InvokeResponse(text="must never be requested", tokens_in=1, tokens_out=1)]
    )

    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(agent.id, worker_id="w")

    db.refresh(agent)
    assert adapter.calls == []
    assert agent.status == AgentRunStatus.FAILED


def test_sequential_workflows_record_labelled_single_source_evidence(
    db, session_factory, bootstrap, worker, calls
):
    built = build_chain(
        db, bootstrap.project, [("first", AGENT), ("second", AGENT), ("result", TERMINAL)], label="e5"
    )
    run = start(db, built)
    drain(worker)
    nodes = snapshot(session_factory, run.id)["node_runs"]
    check = session_factory()
    try:
        context = check.get(AgentRun, nodes["second"].agent_run_id).input_context_json
    finally:
        check.close()
    artifact = nodes["first"].output_snapshot_ref
    assert context == {
        "kind": "workflow_upstream",
        "upstream_artifact_ids": [artifact],
        "upstream_node_run_ids": [nodes["first"].id],
        "upstream": [
            {
                "artifact_id": artifact,
                "node_key": "first",
                "node_run_id": nodes["first"].id,
                "source_node_keys": ["first"],
            }
        ],
    }


def test_gate_approval_fingerprint_is_unchanged_by_canonical_ordering(
    db, session_factory, bootstrap, worker, calls
):
    """MA7.3 fingerprints (and therefore any pending approval) must not change
    across this upgrade: same payload keys, same values."""
    built, run = _waiting(db, session_factory, bootstrap, worker, "e7")
    state = snapshot(session_factory, run.id)
    approval, gate, planner = state["approvals"][0], state["node_runs"]["gate"], state["node_runs"]["planner"]
    check = session_factory()
    try:
        content_hash = check.get(Artifact, planner.output_snapshot_ref).content_hash
    finally:
        check.close()
    assert approval.operation_type == WORKFLOW_HUMAN_APPROVAL_OPERATION
    assert approval.action_fingerprint == compute_action_fingerprint(
        {
            "kind": WORKFLOW_HUMAN_APPROVAL_OPERATION,
            "workflow_run_id": run.id,
            "workflow_node_run_id": gate.id,
            "workflow_node_id": built.nodes["gate"].id,
            "node_key": "gate",
            "iteration": 0,
            "approval_group": "eng-leads",
            "upstream": [
                {
                    "node_run_id": planner.id,
                    "artifact_id": planner.output_snapshot_ref,
                    "artifact_content_hash": content_hash,
                }
            ],
        }
    )


# =============================================================================
# F. Conditional edges
# =============================================================================


def test_publish_rejects_an_edge_with_a_condition(db, bootstrap):
    built = build_diamond(db, bootstrap.project, label="f1", condition_on="security")
    definitions = WorkflowDefinitionService(db)
    with pytest.raises(DAGValidationError) as exc:
        definitions.publish_version(built.workflow.id, built.version.version)
    assert exc.value.issues == [
        "Edge eng -> security has a condition; conditional routing is not supported yet"
    ]


def test_an_empty_condition_object_is_still_a_condition(db, bootstrap):
    definitions = WorkflowDefinitionService(db)
    workflow = definitions.create_workflow(bootstrap.project.id, "f1-empty")
    version = definitions.get_latest_version(workflow.id)
    agent_node = definitions.add_node(
        workflow.id,
        version.version,
        "a",
        WorkflowNodeType.AGENT,
        config={"agent_version_id": make_agent_version(db, make_agent(db, bootstrap.project, "f1e")).id},
    )
    end = definitions.add_node(workflow.id, version.version, "end", WorkflowNodeType.TERMINAL)
    definitions.add_edge(workflow.id, version.version, agent_node.id, end.id, condition={})
    with pytest.raises(DAGValidationError) as exc:
        definitions.publish_version(workflow.id, version.version)
    assert any("a -> end has a condition" in issue for issue in exc.value.issues)


def test_unconditional_edges_still_publish_including_an_explicit_none(db, session_factory, bootstrap):
    """An explicit ``condition=None`` is stored as JSON null (not SQL NULL);
    it must still be recognised as 'no condition' after a reload."""
    built = build_diamond(db, bootstrap.project, label="f3")  # add_edge(..., condition=None) throughout
    assert built.published.status == VersionStatus.ACTIVE
    check = session_factory()
    try:
        from app.models.workflow import WorkflowEdge

        edges = check.query(WorkflowEdge).filter(WorkflowEdge.workflow_version_id == built.published.id).all()
        assert edges and all(e.condition is None for e in edges)
    finally:
        check.close()


def test_publish_endpoint_reports_the_conditional_edge(client, db, auth_headers, bootstrap):
    built = build_diamond(db, bootstrap.project, label="f4", condition_on="test")
    response = client.post(
        f"/workflows/{built.workflow.id}/versions/{built.version.version}/publish", headers=auth_headers
    )
    assert response.status_code == 400
    assert "has a condition" in response.text and "eng -> test" in response.text


def test_a_version_that_carries_a_condition_is_refused_at_start(db, session_factory, bootstrap):
    """Defense in depth for a version published before this validation existed."""
    built = build_diamond(db, bootstrap.project, label="f2", condition_on="code")
    version = db.get(type(built.version), built.version.id)
    version.status = VersionStatus.ACTIVE  # bypasses publish-time validation
    db.commit()
    before = (
        db.query(WorkflowRun).count(),
        db.query(WorkflowNodeRun).count(),
    )
    with pytest.raises(WorkflowExecutionError, match="conditional edges"):
        WorkflowExecutionService(db).start_workflow_run(version.id, built.task_run.id)
    assert (db.query(WorkflowRun).count(), db.query(WorkflowNodeRun).count()) == before


def test_start_endpoint_returns_400_for_a_conditional_version(client, db, auth_headers, bootstrap):
    built = build_diamond(db, bootstrap.project, label="f5", condition_on="code")
    version = db.get(type(built.version), built.version.id)
    version.status = VersionStatus.ACTIVE
    db.commit()
    response = client.post(
        f"/workflows/{built.workflow.id}/versions/{version.version}/runs",
        headers=auth_headers,
        json={"task_run_id": built.task_run.id},
    )
    assert response.status_code == 400 and "conditional" in response.text


# =============================================================================
# G. Sequential behavior intact; fan-out is ENABLED by MA7.4b
# =============================================================================


def test_multi_ready_no_longer_fails_the_run_fan_out_dispatches_every_branch(db, session_factory, bootstrap):
    """MA7.4a pinned "several ready nodes => the run fails closed" because
    fan-out did not exist yet. MA7.4b replaces that with generic fan-out:
    every ready branch is dispatched (see tests/test_ma7_4b_parallel_execution.py
    for the full fan-out/fan-in/failure/cancellation/recovery coverage)."""
    built = build_diamond(db, bootstrap.project, label="g1")
    run = WorkflowExecutionService(db).start_workflow_run(built.published.id, built.task_run.id)
    eng = snapshot(session_factory, run.id)["node_runs"]["eng"]
    agent = db.get(AgentRun, eng.agent_run_id)
    finish_agent(db, agent, AgentRunStatus.COMPLETED)
    WorkflowExecutionService(db).on_agent_run_complete(agent.id)

    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.RUNNING
    assert state["nodes"]["eng"] == WorkflowNodeRunStatus.COMPLETED
    assert all(state["nodes"][k] == WorkflowNodeRunStatus.RUNNING for k in BRANCHES)  # all dispatched
    assert state["nodes"]["join"] == WorkflowNodeRunStatus.PENDING  # the fan-in barrier holds
    assert (state["agent_runs_total"], state["jobs_total"]) == (4, 4)  # eng + 3 branches, once each
    assert "workflow.error" not in event_types(session_factory, built.task_run.id)


def test_a_human_approval_with_two_upstream_dependencies_is_accepted_from_ma7_4c(db, bootstrap):
    """MA7.4a pinned "still rejected -- multi-parent Human Approval is MA7.4c".
    MA7.4c is where it becomes legal; the full behavior is covered in
    tests/test_ma7_4c_parallel_approval.py."""
    definitions = WorkflowDefinitionService(db)
    workflow = definitions.create_workflow(bootstrap.project.id, "g2")
    version = definitions.get_latest_version(workflow.id)
    nodes = {}
    for key in ("a", "b"):
        nodes[key] = definitions.add_node(
            workflow.id,
            version.version,
            key,
            WorkflowNodeType.AGENT,
            config={
                "agent_version_id": make_agent_version(db, make_agent(db, bootstrap.project, f"g2{key}")).id
            },
        )
    gate = definitions.add_node(
        workflow.id, version.version, "gate", WorkflowNodeType.HUMAN_APPROVAL, config={"approval_group": "g"}
    )
    end = definitions.add_node(workflow.id, version.version, "end", WorkflowNodeType.TERMINAL)
    for up, down in (("a", "b"), ("b", "gate"), ("a", "gate")):
        definitions.add_edge(
            workflow.id, version.version, nodes[up].id, (gate if down == "gate" else nodes[down]).id
        )
    definitions.add_edge(workflow.id, version.version, gate.id, end.id)
    assert definitions.publish_version(workflow.id, version.version).status.value == "active"

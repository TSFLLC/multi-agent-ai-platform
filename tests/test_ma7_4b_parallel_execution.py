"""MA7.4b -- generic durable parallel fan-out / fan-in in the MA7 DAG engine.

A. Production-path acceptance: Planner -> Engineer -> {Test, Security, Code}
   -> Aggregator -> TERMINAL through the real Worker, queue, AgentExecutionService
   and TaskRun/AgentRun lifecycle. ONLY the model provider is faked.
B. Generic fan-out (deterministic order, any node kinds, multiple entry nodes,
   asymmetric graphs, nothing role-specific) and exactly-once dispatch under
   concurrency.
C. ALL-of fan-in (the barrier is just a node with several incoming edges).
D. WorkflowRun waiting-state derivation (NODE_WAITING_FOR_APPROVAL is a
   summary, never a lock) and gates as parallel branches.
E. Failure: fail fast + cooperative sibling cancellation.
F. Workflow cancellation across parallel branches.
G. Recovery / reconciliation of every parallel crash boundary.
H. Multi-parent Agent context.
I. Maximum fan-out (publish + start), conditional edges, unsupported node types.
J. Sequential MA7.2 / MA7.3 behavior unchanged.

Every database is a disposable temp file; nothing here opens data/*.db.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import Settings, settings
from app.db.enums import (
    AgentRunStatus,
    ApprovalStatus,
    JobQueueStatus,
    JobType,
    TaskRunStatus,
    VersionStatus,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
    WorkflowRunStatus,
)
from app.models.artifacts_eval import Artifact
from app.models.execution import JobQueue
from app.models.tasks import AgentRun, AgentRunAttempt, TaskRun
from app.models.workflow import WorkflowNode, WorkflowNodeRun, WorkflowRun
from app.providers.base import ProviderInvalidResponseError
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.workflow_execution_service import WorkflowExecutionError, WorkflowExecutionService
from app.services.workflow_validation_service import DAGValidationError
from tests.conftest import make_agent, make_agent_version, make_task
from tests.ma7_3b_support import (
    AGENT,
    APPROVAL,
    TERMINAL,
    build_chain,
    drain,
    event_types,
    events,
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
    single_unsupported_node,
)
from tests.ma7_4b_support import (
    REVIEWERS,
    RoleProvider,
    build_acceptance,
    build_graph,
    build_star,
    install_provider,
    new_worker,
    running_workers,
    wait_until,
)

ACTIVE_RUN = (WorkflowRunStatus.RUNNING, WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL)


@pytest.fixture()
def calls(monkeypatch, tmp_path):
    """Stubbed-inference agents (orchestration tests): each agent run completes
    with a real artifact file + sha256; ``calls`` collects node keys in
    execution order. The production-path tests below use RoleProvider instead."""
    return install_fake_agents(monkeypatch, tmp_path)


@pytest.fixture()
def worker(session_factory):
    return new_worker(session_factory)


@pytest.fixture()
def artifacts_dir(monkeypatch, tmp_path):
    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


# -- small helpers -------------------------------------------------------------------


def nodes_of(session_factory, run_id):
    return snapshot(session_factory, run_id)["nodes"]


def run_of(session_factory, run_id):
    return snapshot(session_factory, run_id)["run"]


def agent_of(db, session_factory, run_id, key):
    node_run = snapshot(session_factory, run_id)["node_runs"][key]
    agent = db.get(AgentRun, node_run.agent_run_id)
    db.refresh(agent)
    return agent


def finish(db, session_factory, run_id, key, status=AgentRunStatus.COMPLETED, *, tmp_path=None):
    """The agent of node ``key`` reaches a terminal status (no callback yet)."""
    agent = agent_of(db, session_factory, run_id, key)
    finish_agent(db, agent, status, text=f"OUT-{key}", tmp_path=tmp_path)
    return agent


def complete(db, session_factory, run_id, key, status=AgentRunStatus.COMPLETED, *, tmp_path=None):
    """...and the worker's completion callback runs."""
    agent = finish(db, session_factory, run_id, key, status, tmp_path=tmp_path)
    WorkflowExecutionService(db).on_agent_run_complete(agent.id)
    return agent


def start_fanned_out_diamond(db, session_factory, project, tmp_path, label):
    """eng -> {code, security, test} -> join -> end, started, with eng complete:
    the three branches are dispatched and RUNNING (jobs queued, never run)."""
    built = build_diamond(db, project, label=label)
    run = start(db, built)
    complete(db, session_factory, run.id, "eng", tmp_path=tmp_path)
    return built, run


def dispatch_order(session_factory, run_id):
    """Node keys in the order their Agent jobs were enqueued."""
    session = session_factory()
    try:
        by_agent = {
            nr.agent_run_id: session.get(WorkflowNode, nr.workflow_node_id).node_key
            for nr in session.query(WorkflowNodeRun).filter(WorkflowNodeRun.workflow_run_id == run_id)
            if nr.agent_run_id
        }
        jobs = (
            session.query(JobQueue)
            .filter(JobQueue.job_type == JobType.AGENT_RUN, JobQueue.payload_ref.in_(list(by_agent)))
            .order_by(JobQueue.created_at)
            .all()
        )
        return [by_agent[job.payload_ref] for job in jobs]
    finally:
        session.close()


def count_events(session_factory, task_run_id, event_type, node_run_id=None):
    return len(
        [e for e in events(session_factory, task_run_id) if e[0] == event_type and node_run_id in (None, e[3])]
    )


def rows(session_factory, model, *criteria):
    session = session_factory()
    try:
        return session.query(model).filter(*criteria).all()
    finally:
        session.close()


def totals(state):
    """(AgentRuns, TaskRuns, jobs) in the whole temp database."""
    return (state["agent_runs_total"], state["task_runs_total"], state["jobs_total"])


def delta(before, after):
    """How many AgentRuns / TaskRuns / jobs appeared between two snapshots --
    scoped to the step under test, so a loop over several runs in one
    database stays exact."""
    return tuple(a - b for a, b in zip(totals(after), totals(before)))


def in_threads(count, work, *, timeout=60):
    """Runs ``work(i)`` on ``count`` threads released together by a barrier;
    re-raises the first exception."""
    barrier = threading.Barrier(count, timeout=20)

    def wrapped(i):
        barrier.wait()
        return work(i)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(wrapped, i) for i in range(count)]
        return [future.result(timeout=timeout) for future in futures]


def service_on(session_factory):
    session = session_factory()
    return session, WorkflowExecutionService(session)


# =============================================================================
# A. Production-path acceptance
# =============================================================================


def test_acceptance_planner_engineer_three_parallel_reviewers_aggregator(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    graph = build_acceptance(db, bootstrap.project)
    provider = install_provider(monkeypatch, RoleProvider(graph.role_of))

    # All three reviewers must be inside the provider AT THE SAME TIME (the
    # barrier only opens if they are -- a serial engine would time out), and
    # the code reviewer is then held so we can look at the run mid-flight.
    meeting = threading.Barrier(3, timeout=20)
    release_code = threading.Event()
    hook_errors = []

    def meet(_request):
        try:
            meeting.wait()
        except Exception as exc:  # pragma: no cover - only on a regression
            hook_errors.append(exc)
            raise

    def code_reviewer(request):
        meet(request)
        assert release_code.wait(30)

    provider.hooks.update(test_engineer=meet, security_reviewer=meet, code_reviewer=code_reviewer)

    run = start(db, graph.built)
    solo = new_worker(session_factory)

    assert solo.run_once() is True  # Planner
    after_planner = snapshot(session_factory, run.id)
    assert after_planner["nodes"]["planner"] == WorkflowNodeRunStatus.COMPLETED
    assert after_planner["nodes"]["engineer"] == WorkflowNodeRunStatus.RUNNING
    assert provider.roles() == ["planner"]

    assert solo.run_once() is True  # Engineer
    fanned = snapshot(session_factory, run.id)
    assert provider.roles() == ["planner", "engineer"]  # each executed once so far
    assert {fanned["nodes"][key] for key in REVIEWERS} == {WorkflowNodeRunStatus.RUNNING}  # independently runnable
    assert fanned["nodes"]["aggregator"] == WorkflowNodeRunStatus.PENDING
    assert fanned["nodes"]["result"] == WorkflowNodeRunStatus.PENDING
    assert fanned["run"] == WorkflowRunStatus.RUNNING
    assert dispatch_order(session_factory, run.id)[2:] == list(REVIEWERS)  # deterministic node_key order

    with running_workers(session_factory, 3):
        # Test + Security finish while Code is still in flight: no sibling waited for another...
        assert wait_until(
            lambda: {
                k: v
                for k, v in nodes_of(session_factory, run.id).items()
                if k in ("test_engineer", "security_reviewer")
            }
            == {
                "test_engineer": WorkflowNodeRunStatus.COMPLETED,
                "security_reviewer": WorkflowNodeRunStatus.COMPLETED,
            }
        )
        mid = snapshot(session_factory, run.id)
        assert mid["nodes"]["code_reviewer"] == WorkflowNodeRunStatus.RUNNING
        # ...and the fan-in barrier holds: 2 of 3 complete => Aggregator is NOT dispatched
        assert mid["nodes"]["aggregator"] == WorkflowNodeRunStatus.PENDING
        assert mid["node_runs"]["aggregator"].agent_run_id is None
        assert provider.count("aggregator") == 0
        assert mid["run"] == WorkflowRunStatus.RUNNING

        release_code.set()
        assert wait_until(lambda: run_of(session_factory, run.id) == WorkflowRunStatus.COMPLETED)

    assert hook_errors == []
    final = snapshot(session_factory, run.id)

    # every node exactly once, Aggregator last, in a legal order
    roles = provider.roles()
    assert sorted(roles) == sorted(
        ["planner", "engineer", "test_engineer", "security_reviewer", "code_reviewer", "aggregator"]
    )
    assert roles[:2] == ["planner", "engineer"] and roles[-1] == "aggregator"

    assert set(final["nodes"].values()) == {WorkflowNodeRunStatus.COMPLETED}  # incl. TERMINAL
    assert final["run"] == WorkflowRunStatus.COMPLETED and final["run_ended_at"] is not None

    # Aggregator received ALL three outputs, labelled, in canonical (node_key) order -- and only those
    prompt = provider.prompt_of("aggregator")
    labels = [f"source_node: {key}" for key in REVIEWERS]
    positions = [prompt.index(label) for label in labels]
    assert positions == sorted(positions)
    for key in REVIEWERS:
        artifact_id = final["node_runs"][key].output_snapshot_ref
        assert f"OUTPUT-OF-{key}" in prompt
        assert f"artifact_id: {artifact_id}" in prompt
        check = session_factory()
        try:
            assert f"artifact_sha256: {check.get(Artifact, artifact_id).content_hash}" in prompt
        finally:
            check.close()
    assert "OUTPUT-OF-engineer" not in prompt and "OUTPUT-OF-planner" not in prompt

    # exactly-once bookkeeping: 6 agent nodes -> 6 AgentRuns / 6 node TaskRuns (+1 parent) / 6 jobs
    assert final["agent_runs_total"] == 6
    assert final["task_runs_total"] == 7
    assert final["jobs_total"] == 6
    assert len(set(final["agent_run_ids"])) == 6
    assert {job.status for job in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE}
    assert {a.status for a in rows(session_factory, AgentRun)} == {AgentRunStatus.COMPLETED}
    assert len(rows(session_factory, AgentRunAttempt)) == 6  # one attempt each: nothing re-ran
    assert {t.status for t in rows(session_factory, TaskRun, TaskRun.id != graph.built.task_run.id)} == {
        TaskRunStatus.COMPLETED
    }
    parent = graph.built.task_run.id
    assert count_events(session_factory, parent, "workflow.started") == 1
    assert count_events(session_factory, parent, "workflow.node.scheduled") == 6
    assert count_events(session_factory, parent, "workflow.completed") == 1


# =============================================================================
# B. Generic fan-out
# =============================================================================


def test_fan_out_dispatches_every_ready_branch_once_in_node_key_order(
    db, session_factory, bootstrap, tmp_path
):
    built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, "b1")
    state = snapshot(session_factory, run.id)

    assert {state["nodes"][key] for key in BRANCHES} == {WorkflowNodeRunStatus.RUNNING}
    assert state["nodes"]["join"] == WorkflowNodeRunStatus.PENDING
    assert dispatch_order(session_factory, run.id) == ["eng", "code", "security", "test"]
    assert (state["agent_runs_total"], state["task_runs_total"], state["jobs_total"]) == (4, 5, 4)
    assert state["run"] == WorkflowRunStatus.RUNNING
    assert "workflow.error" not in event_types(session_factory, built.task_run.id)


def test_parallelism_is_generic_not_role_specific(db, session_factory, bootstrap, worker, calls):
    """Planner -> {Backend, Frontend, Database, Documentation} -> Integration:
    same engine, arbitrary names, one more branch than the acceptance graph."""
    keys = ["backend", "database", "documentation", "frontend"]
    nodes = [("planner", AGENT)] + [(k, AGENT) for k in keys] + [("integration", AGENT), ("done", TERMINAL)]
    edges = [("planner", k) for k in keys] + [(k, "integration") for k in keys] + [("integration", "done")]
    graph = build_graph(db, bootstrap.project, nodes, edges, label="gen")
    run = start(db, graph.built)

    drain(worker)

    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.COMPLETED
    assert sorted(calls) == sorted(["planner", *keys, "integration"])  # every node exactly once
    assert calls[0] == "planner" and calls[-1] == "integration"
    check = session_factory()
    try:
        context = check.get(AgentRun, state["node_runs"]["integration"].agent_run_id).input_context_json
    finally:
        check.close()
    assert [entry["node_key"] for entry in context["upstream"]] == keys  # all four, canonical order


def test_a_graph_may_have_several_entry_nodes(db, session_factory, bootstrap, worker, calls):
    nodes = [("left", AGENT), ("right", AGENT), ("merge", AGENT), ("end", TERMINAL)]
    edges = [("left", "merge"), ("right", "merge"), ("merge", "end")]
    graph = build_graph(db, bootstrap.project, nodes, edges, label="entries")
    run = start(db, graph.built)
    assert {k: v for k, v in nodes_of(session_factory, run.id).items() if k in ("left", "right")} == {
        "left": WorkflowNodeRunStatus.RUNNING,
        "right": WorkflowNodeRunStatus.RUNNING,
    }  # both entry nodes started together
    drain(worker)
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED
    assert calls[-1] == "merge" and sorted(calls) == ["left", "merge", "right"]


def test_asymmetric_dag_branches_of_different_length_and_kind(db, session_factory, bootstrap, tmp_path):
    """p -> {short, chain1 -> chain2 -> chain3} -> join: the short branch does
    not wait for the long one, and the join waits for BOTH."""
    nodes = [(k, AGENT) for k in ("p", "short", "chain1", "chain2", "chain3", "join")] + [("end", TERMINAL)]
    edges = [
        ("p", "short"),
        ("p", "chain1"),
        ("chain1", "chain2"),
        ("chain2", "chain3"),
        ("short", "join"),
        ("chain3", "join"),
        ("join", "end"),
    ]
    graph = build_graph(db, bootstrap.project, nodes, edges, label="asym")
    run = start(db, graph.built)
    complete(db, session_factory, run.id, "p", tmp_path=tmp_path)
    assert dispatch_order(session_factory, run.id) == ["p", "chain1", "short"]

    complete(db, session_factory, run.id, "short", tmp_path=tmp_path)  # finishes first, alone
    state = nodes_of(session_factory, run.id)
    assert state["short"] == WorkflowNodeRunStatus.COMPLETED and state["chain1"] == WorkflowNodeRunStatus.RUNNING
    assert state["join"] == WorkflowNodeRunStatus.PENDING

    for key in ("chain1", "chain2"):
        complete(db, session_factory, run.id, key, tmp_path=tmp_path)
        assert nodes_of(session_factory, run.id)["join"] == WorkflowNodeRunStatus.PENDING  # still one source short
    assert nodes_of(session_factory, run.id)["chain3"] == WorkflowNodeRunStatus.RUNNING

    complete(db, session_factory, run.id, "chain3", tmp_path=tmp_path)
    final = snapshot(session_factory, run.id)
    assert final["nodes"]["join"] == WorkflowNodeRunStatus.RUNNING
    join_agent = db.get(AgentRun, final["node_runs"]["join"].agent_run_id)
    assert [entry["node_key"] for entry in join_agent.input_context_json["upstream"]] == ["chain3", "short"]


def test_concurrent_schedulers_and_reconcilers_dispatch_each_branch_exactly_once(
    db, session_factory, bootstrap, tmp_path, monkeypatch
):
    """After the fan-out point every branch is dispatched once even when
    schedulers, reconcilers and completion replays all race for it. Repeated
    with fresh graphs to expose flakiness."""
    for round_number in range(6):
        built = build_diamond(db, bootstrap.project, label=f"b5-{round_number}")
        run = start(db, built)
        eng = finish(db, session_factory, run.id, "eng", tmp_path=tmp_path)  # completed, callback NOT run
        before = snapshot(session_factory, run.id)

        def contend(i, run_id=run.id, eng_id=eng.id):
            session, service = service_on(session_factory)
            try:
                if i % 3 == 0:
                    service.on_agent_run_complete(eng_id)
                elif i % 3 == 1:
                    service._schedule_ready_nodes(run_id)
                else:
                    service.reconcile_workflow_run(run_id)
            finally:
                session.close()

        in_threads(6, contend)

        state = snapshot(session_factory, run.id)
        assert {state["nodes"][k] for k in BRANCHES} == {WorkflowNodeRunStatus.RUNNING}, round_number
        # exactly three new AgentRuns / TaskRuns / jobs -- one per branch -- and nothing orphaned
        assert delta(before, state) == (3, 3, 3), round_number
        assert len(state["agent_run_ids"]) == len(set(state["agent_run_ids"])) == 4
        assert count_events(session_factory, built.task_run.id, "workflow.node.scheduled") == 4  # eng + 3


def test_a_lost_dispatch_race_leaves_no_orphan_rows(db, session_factory, bootstrap, tmp_path):
    built = build_diamond(db, bootstrap.project, label="b6")
    run, node_runs = manual_run(db, built)
    complete_node(db, built, node_runs, "eng", text="E", tmp_path=tmp_path)  # 'code' is ready, undispatched
    before = snapshot(session_factory, run.id)

    def dispatch(_):
        session, service = service_on(session_factory)
        try:
            service._dispatch_node_for_execution(session.get(WorkflowNodeRun, node_runs["code"].id))
        finally:
            session.close()

    in_threads(5, dispatch)

    after = snapshot(session_factory, run.id)
    assert after["nodes"]["code"] == WorkflowNodeRunStatus.RUNNING
    # five dispatchers raced: ONE TaskRun/AgentRun/job survived; the four losers left nothing behind
    assert delta(before, after) == (1, 1, 1)


# =============================================================================
# C. ALL-of fan-in
# =============================================================================


def test_fan_in_node_becomes_ready_only_when_every_source_is_completed(
    db, session_factory, bootstrap, tmp_path
):
    _built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, "c1")
    complete(db, session_factory, run.id, "test", tmp_path=tmp_path)
    complete(db, session_factory, run.id, "security", tmp_path=tmp_path)
    state = snapshot(session_factory, run.id)
    assert state["nodes"]["code"] == WorkflowNodeRunStatus.RUNNING  # one still running
    assert state["nodes"]["join"] == WorkflowNodeRunStatus.PENDING  # -> the barrier holds
    assert state["node_runs"]["join"].agent_run_id is None
    assert state["agent_runs_total"] == 4 and state["jobs_total"] == 4

    complete(db, session_factory, run.id, "code", tmp_path=tmp_path)  # the last source
    state = snapshot(session_factory, run.id)
    assert state["nodes"]["join"] == WorkflowNodeRunStatus.RUNNING
    assert state["agent_runs_total"] == 5 and state["jobs_total"] == 5
    assert dispatch_order(session_factory, run.id)[-1] == "join"


@pytest.mark.parametrize("last", BRANCHES)
def test_whichever_source_finishes_last_dispatches_the_fan_in(db, session_factory, bootstrap, tmp_path, last):
    _built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, f"c2-{last}")
    for key in (k for k in BRANCHES if k != last):
        complete(db, session_factory, run.id, key, tmp_path=tmp_path)
    assert nodes_of(session_factory, run.id)["join"] == WorkflowNodeRunStatus.PENDING
    complete(db, session_factory, run.id, last, tmp_path=tmp_path)
    assert nodes_of(session_factory, run.id)["join"] == WorkflowNodeRunStatus.RUNNING


def test_simultaneous_sibling_completions_dispatch_the_fan_in_exactly_once(
    db, session_factory, bootstrap, tmp_path
):
    """All three branches finish and report at the same instant, from three
    threads. Repeated: a lost wake-up or a double dispatch shows up as a
    stuck run or a second Aggregator."""
    for round_number in range(10):
        built, run = start_fanned_out_diamond(
            db, session_factory, bootstrap.project, tmp_path, f"c3-{round_number}"
        )
        agents = {key: finish(db, session_factory, run.id, key, tmp_path=tmp_path) for key in BRANCHES}
        before = snapshot(session_factory, run.id)

        def report(i, agents=agents):
            session, service = service_on(session_factory)
            try:
                service.on_agent_run_complete(agents[BRANCHES[i]].id)
            finally:
                session.close()

        in_threads(3, report)

        state = snapshot(session_factory, run.id)
        assert {state["nodes"][k] for k in BRANCHES} == {WorkflowNodeRunStatus.COMPLETED}, round_number
        assert state["nodes"]["join"] == WorkflowNodeRunStatus.RUNNING, round_number
        assert delta(before, state) == (1, 1, 1), round_number  # exactly ONE Aggregator dispatch
        parent = built.task_run.id
        for key in BRANCHES:  # each completion recorded exactly once
            assert count_events(session_factory, parent, "workflow.node.completed", state["node_runs"][key].id) == 1
        assert count_events(session_factory, parent, "workflow.node.scheduled", state["node_runs"]["join"].id) == 1


def test_duplicate_completion_callbacks_change_nothing(db, session_factory, bootstrap, tmp_path):
    built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, "c4")
    agents = {key: finish(db, session_factory, run.id, key, tmp_path=tmp_path) for key in BRANCHES}
    service = WorkflowExecutionService(db)
    for key in BRANCHES:
        service.on_agent_run_complete(agents[key].id)
    settled = snapshot(session_factory, run.id)
    types_before = event_types(session_factory, built.task_run.id)

    for _ in range(4):
        for key in BRANCHES:
            service.on_agent_run_complete(agents[key].id)  # replayed worker callbacks

    after = snapshot(session_factory, run.id)
    assert after["nodes"] == settled["nodes"]
    assert (after["agent_runs_total"], after["task_runs_total"], after["jobs_total"]) == (5, 6, 5)
    assert event_types(session_factory, built.task_run.id) == types_before


def test_all_branches_complete_but_the_fan_in_was_never_dispatched(
    db, session_factory, bootstrap, tmp_path, worker
):
    """A process died after the last branch's completion committed and before
    it dispatched the join. Reconciliation dispatches it -- once, however
    often and from however many processes it runs."""
    _built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, "c5")
    for key in BRANCHES:
        finish(db, session_factory, run.id, key, tmp_path=tmp_path)  # agents done; no callback ever ran
    assert nodes_of(session_factory, run.id)["join"] == WorkflowNodeRunStatus.PENDING

    for _ in range(3):
        worker.reconcile_workflows()
    in_threads(4, lambda _i: new_worker(session_factory).reconcile_workflows())

    state = snapshot(session_factory, run.id)
    assert {state["nodes"][k] for k in BRANCHES} == {WorkflowNodeRunStatus.COMPLETED}
    assert state["nodes"]["join"] == WorkflowNodeRunStatus.RUNNING
    assert (state["agent_runs_total"], state["task_runs_total"], state["jobs_total"]) == (5, 6, 5)


# =============================================================================
# D. WorkflowRun waiting-state semantics
# =============================================================================

GATE_BRANCH = (
    [("entry", AGENT), ("a", AGENT), ("gate", APPROVAL), ("z", AGENT), ("end", TERMINAL)],
    [("entry", "a"), ("entry", "gate"), ("entry", "z"), ("a", "end"), ("gate", "end"), ("z", "end")],
)


def gate_graph(db, project, label):
    nodes, edges = GATE_BRANCH
    return build_graph(db, project, nodes, edges, label=label)


def test_a_waiting_gate_does_not_stop_independent_branches_from_progressing(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    graph = gate_graph(db, bootstrap.project, "d1")
    run = start(db, graph.built)
    assert worker.run_once() is True  # entry -> fans out to a, gate, z

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert (state["nodes"]["a"], state["nodes"]["z"]) == (WorkflowNodeRunStatus.RUNNING,) * 2
    assert state["run"] == WorkflowRunStatus.RUNNING  # a gate is waiting, but the run can still progress

    assert worker.run_once() is True  # a
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.RUNNING  # z is still running
    assert worker.run_once() is True  # z: now ONLY the human is left
    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert calls == ["entry", "a", "z"]

    resolve_via_api(client, auth_headers, state["approvals"][0])
    state = snapshot(session_factory, run.id)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED
    assert state["run"] == WorkflowRunStatus.COMPLETED  # end (fan-in of a, gate, z) dispatched and finished


def test_the_summary_status_follows_the_work_not_the_other_way_round(
    db, session_factory, bootstrap, tmp_path
):
    """NODE_WAITING_FOR_APPROVAL is re-derived after dispatch, completion and
    reconciliation: RUNNING while anything can progress, waiting only when a
    human is all that is left -- and never a lock."""
    graph = gate_graph(db, bootstrap.project, "d2")
    run = start(db, graph.built)
    complete(db, session_factory, run.id, "entry", tmp_path=tmp_path)
    assert run_of(session_factory, run.id) == WorkflowRunStatus.RUNNING  # gate waiting; a, z running

    complete(db, session_factory, run.id, "a", tmp_path=tmp_path)
    assert run_of(session_factory, run.id) == WorkflowRunStatus.RUNNING  # z still running
    complete(db, session_factory, run.id, "z", tmp_path=tmp_path)
    assert run_of(session_factory, run.id) == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL

    service = WorkflowExecutionService(db)
    for _ in range(3):  # re-deriving never flaps
        service._sync_run_state(run.id)
        service.reconcile_workflow_run(run.id)
    assert run_of(session_factory, run.id) == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL


def test_a_node_waiting_summary_status_never_blocks_a_ready_node(db, session_factory, bootstrap, tmp_path):
    """The old MA7.3 test pinned "a NODE_WAITING run never dispatches". Now
    that status is only a summary: a ready node is dispatched whatever the
    (possibly stale) summary says, and the summary is corrected."""
    built = build_diamond(db, bootstrap.project, label="d3")
    run, node_runs = manual_run(db, built, status=WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL)
    complete_node(db, built, node_runs, "eng", text="E", tmp_path=tmp_path)

    session, service = service_on(session_factory)
    try:
        service._schedule_ready_nodes(run.id)
    finally:
        session.close()

    state = snapshot(session_factory, run.id)
    assert {state["nodes"][k] for k in BRANCHES} == {WorkflowNodeRunStatus.RUNNING}
    assert state["run"] == WorkflowRunStatus.RUNNING


@pytest.mark.parametrize("status", ["cancelling", "failed", "cancelled", "completed"])
def test_no_other_run_status_dispatches_a_ready_branch(db, session_factory, bootstrap, tmp_path, status):
    built = build_diamond(db, bootstrap.project, label=f"d4-{status}")
    run, node_runs = manual_run(db, built, status=WorkflowRunStatus(status))
    complete_node(db, built, node_runs, "eng", text="E", tmp_path=tmp_path)
    before = snapshot(session_factory, run.id)
    session, service = service_on(session_factory)
    try:
        service._schedule_ready_nodes(run.id)
    finally:
        session.close()
    state = snapshot(session_factory, run.id)
    assert {state["nodes"][k] for k in BRANCHES} == {WorkflowNodeRunStatus.PENDING}
    assert delta(before, state) == (0, 0, 0)


def test_a_rejected_gate_beside_a_running_branch_fails_fast_and_drains(
    client, db, session_factory, auth_headers, bootstrap, worker, calls, tmp_path
):
    graph = gate_graph(db, bootstrap.project, "d5")
    run = start(db, graph.built)
    worker.run_once()  # entry: a, gate (waiting), z
    approval = snapshot(session_factory, run.id)["approvals"][0]

    resolve_via_api(client, auth_headers, approval, approve=False)

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.FAILED
    assert state["run"] == WorkflowRunStatus.RUNNING  # a and z are still draining -- not finalized yet
    assert state["run_ended_at"] is None
    for key in ("a", "z"):  # asked to stop
        assert agent_of(db, session_factory, run.id, key).cancellation_requested_at is not None

    for key in ("a", "z"):
        agent = finish(db, session_factory, run.id, key, AgentRunStatus.STOPPED)
        WorkflowExecutionService(db).on_agent_run_complete(agent.id)
    final = snapshot(session_factory, run.id)
    assert final["run"] == WorkflowRunStatus.FAILED and final["run_ended_at"] is not None
    assert final["nodes"]["end"] == WorkflowNodeRunStatus.PENDING  # never dispatched
    assert count_events(session_factory, graph.built.task_run.id, "workflow.failed") == 1


def test_an_agent_failure_expires_a_waiting_gate_with_reason_workflow_failed(
    client, db, session_factory, auth_headers, bootstrap, worker, calls, tmp_path
):
    graph = gate_graph(db, bootstrap.project, "d6")
    run = start(db, graph.built)
    worker.run_once()  # entry: a, gate (waiting), z
    approval = snapshot(session_factory, run.id)["approvals"][0]

    complete(db, session_factory, run.id, "a", AgentRunStatus.FAILED)

    state = snapshot(session_factory, run.id)
    gate_approval = state["approvals"][0]
    assert gate_approval.status == ApprovalStatus.EXPIRED  # never REJECTED: nobody decided
    assert gate_approval.resolution_note == "workflow_failed"
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.CANCELLED
    assert state["run"] == WorkflowRunStatus.RUNNING  # z is still draining

    # the closed gate can no longer be approved into a failed run
    assert resolve_via_api(client, auth_headers, approval).status_code == 409
    assert snapshot(session_factory, run.id)["nodes"]["gate"] == WorkflowNodeRunStatus.CANCELLED

    complete(db, session_factory, run.id, "z", AgentRunStatus.STOPPED)
    assert run_of(session_factory, run.id) == WorkflowRunStatus.FAILED


# =============================================================================
# E. Failure: fail fast + cooperative sibling cancellation
# =============================================================================


def test_branch_failure_cancels_queued_siblings_and_never_dispatches_the_fan_in(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    graph = build_acceptance(db, bootstrap.project, label="e1")
    provider = install_provider(monkeypatch, RoleProvider(graph.role_of))
    provider.hooks["code_reviewer"] = lambda _r: (_ for _ in ()).throw(ProviderInvalidResponseError("boom"))
    run = start(db, graph.built)
    solo = new_worker(session_factory)
    solo.run_once()  # planner
    solo.run_once()  # engineer -> three queued branches (jobs in node_key order)

    assert solo.run_once() is True  # code_reviewer: a REAL execution failure
    state = snapshot(session_factory, run.id)
    assert state["nodes"]["code_reviewer"] == WorkflowNodeRunStatus.FAILED
    assert state["run"] == WorkflowRunStatus.RUNNING  # siblings still queued: not drained yet
    for key in ("security_reviewer", "test_engineer"):
        assert state["nodes"][key] == WorkflowNodeRunStatus.RUNNING
        assert agent_of(db, session_factory, run.id, key).cancellation_requested_at is not None

    assert solo.run_once() is True  # security_reviewer's queued job: cancelled at its first checkpoint
    assert solo.run_once() is True  # test_engineer's
    assert solo.run_once() is False

    final = snapshot(session_factory, run.id)
    assert final["run"] == WorkflowRunStatus.FAILED and final["run_ended_at"] is not None
    assert final["nodes"]["security_reviewer"] == WorkflowNodeRunStatus.CANCELLED
    assert final["nodes"]["test_engineer"] == WorkflowNodeRunStatus.CANCELLED
    assert final["nodes"]["aggregator"] == WorkflowNodeRunStatus.PENDING  # never dispatched
    assert final["nodes"]["result"] == WorkflowNodeRunStatus.PENDING
    assert provider.roles() == ["planner", "engineer", "code_reviewer"]  # the queued siblings never called the model
    assert (final["agent_runs_total"], final["jobs_total"]) == (5, 5)  # no Aggregator AgentRun/job
    parent = graph.built.task_run.id
    assert count_events(session_factory, parent, "workflow.failed") == 1
    assert count_events(session_factory, parent, "workflow.node.failed") == 1
    stopped = {a.status for a in rows(session_factory, AgentRun, AgentRun.id.in_(final["agent_run_ids"]))}
    assert AgentRunStatus.STOPPED in stopped and AgentRunStatus.FAILED in stopped


def test_in_flight_sibling_is_cancelled_and_a_completed_sibling_is_preserved(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    graph = build_acceptance(db, bootstrap.project, label="e2")
    provider = install_provider(monkeypatch, RoleProvider(graph.role_of))
    security_in_flight = threading.Event()
    release_security = threading.Event()
    fail_code = threading.Event()

    def security(_request):
        security_in_flight.set()
        assert release_security.wait(30)

    def code(_request):
        assert security_in_flight.wait(30) and fail_code.wait(30)
        raise ProviderInvalidResponseError("boom")

    provider.hooks.update(security_reviewer=security, code_reviewer=code)  # test_engineer: immediate
    run = start(db, graph.built)
    solo = new_worker(session_factory)
    solo.run_once()
    solo.run_once()

    with running_workers(session_factory, 3):
        assert wait_until(
            lambda: nodes_of(session_factory, run.id)["test_engineer"] == WorkflowNodeRunStatus.COMPLETED
            and security_in_flight.is_set()
        )
        fail_code.set()
        assert wait_until(lambda: nodes_of(session_factory, run.id)["code_reviewer"] == WorkflowNodeRunStatus.FAILED)

        # the in-flight sibling was signalled but is mid-call: the run has NOT finalized
        assert wait_until(lambda: agent_of(db, session_factory, run.id, "security_reviewer").cancellation_requested_at)
        mid = snapshot(session_factory, run.id)
        assert mid["nodes"]["security_reviewer"] == WorkflowNodeRunStatus.RUNNING
        assert mid["run"] == WorkflowRunStatus.RUNNING and mid["run_ended_at"] is None
        assert mid["nodes"]["aggregator"] == WorkflowNodeRunStatus.PENDING

        release_security.set()  # its provider call returns; it observes the request at the next checkpoint
        assert wait_until(lambda: run_of(session_factory, run.id) == WorkflowRunStatus.FAILED)

    final = snapshot(session_factory, run.id)
    assert final["run_ended_at"] is not None
    assert final["nodes"]["test_engineer"] == WorkflowNodeRunStatus.COMPLETED  # preserved
    assert final["node_runs"]["test_engineer"].output_snapshot_ref is not None
    assert final["nodes"]["security_reviewer"] == WorkflowNodeRunStatus.CANCELLED
    assert final["nodes"]["code_reviewer"] == WorkflowNodeRunStatus.FAILED
    assert final["nodes"]["aggregator"] == WorkflowNodeRunStatus.PENDING
    assert provider.count("aggregator") == 0
    security_agent = agent_of(db, session_factory, run.id, "security_reviewer")
    assert security_agent.status == AgentRunStatus.STOPPED
    # what the cancelled sibling had already produced is kept (its cost was real)
    assert len(rows(session_factory, Artifact, Artifact.agent_run_id == security_agent.id)) == 1


def test_a_reviewer_reporting_a_defect_is_not_an_execution_failure(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    """"I found a SQL injection" is a successful, COMPLETED agent run whose
    CONTENT is bad news -- the workflow carries on to the Aggregator."""
    graph = build_acceptance(db, bootstrap.project, label="e3")
    provider = install_provider(
        monkeypatch,
        RoleProvider(
            graph.role_of,
            text=lambda role: "CRITICAL: I found a SQL injection vulnerability." if role == "security_reviewer" else f"OK-{role}",
        ),
    )
    run = start(db, graph.built)
    with running_workers(session_factory, 2):
        assert wait_until(lambda: run_of(session_factory, run.id) == WorkflowRunStatus.COMPLETED)
    final = snapshot(session_factory, run.id)
    assert final["nodes"]["security_reviewer"] == WorkflowNodeRunStatus.COMPLETED
    assert "SQL injection" in provider.prompt_of("aggregator")  # the finding reaches the Aggregator
    assert final["run"] == WorkflowRunStatus.COMPLETED


def test_failure_propagation_is_durable_and_the_barrier_needs_no_flag(
    db, session_factory, bootstrap, tmp_path, monkeypatch
):
    """Nothing dispatches once a node has FAILED -- refused by the claim itself,
    so it holds even for a dispatcher that read "ready" before the failure and
    skipped every pre-check."""
    built = build_diamond(db, bootstrap.project, label="e4")
    run, node_runs = manual_run(db, built)
    complete_node(db, built, node_runs, "eng", text="E", tmp_path=tmp_path)
    db.execute(text("UPDATE workflow_node_runs SET status='failed' WHERE id=:id"), {"id": node_runs["test"].id})
    db.commit()
    baseline = snapshot(session_factory, run.id)

    monkeypatch.setattr(WorkflowExecutionService, "_accepts_dispatch", lambda self, run_id: True)  # bypass pre-checks
    session, service = service_on(session_factory)
    try:
        for key in ("code", "security"):
            service._dispatch_node_for_execution(session.get(WorkflowNodeRun, node_runs[key].id))
    finally:
        session.close()

    after = snapshot(session_factory, run.id)
    assert after["nodes"]["code"] == after["nodes"]["security"] == WorkflowNodeRunStatus.PENDING
    assert (after["agent_runs_total"], after["task_runs_total"], after["jobs_total"]) == (
        baseline["agent_runs_total"],
        baseline["task_runs_total"],
        baseline["jobs_total"],
    )  # the refused dispatches left no orphan TaskRun/AgentRun/job behind


@pytest.mark.parametrize(
    ("status", "dispatchable"),
    [
        ("running", True),
        ("node_waiting_for_approval", True),  # a summary, not a lock
        ("created", False),
        ("cancelling", False),
        ("cancelled", False),
        ("failed", False),
        ("completed", False),
    ],
)
def test_the_claim_itself_only_succeeds_for_a_dispatchable_run(
    db, session_factory, bootstrap, status, dispatchable
):
    """The atomic gate, in isolation: whatever pre-checks a dispatcher did, the
    UPDATE refuses unless the run is RUNNING / NODE_WAITING_FOR_APPROVAL."""
    built = build_diamond(db, bootstrap.project, label=f"e7-{status}")
    _run, node_runs = manual_run(db, built, status=WorkflowRunStatus(status))
    session, service = service_on(session_factory)
    try:
        won = service._claim_pending_node(node_runs["eng"].id, status=WorkflowNodeRunStatus.RUNNING)
        session.commit()
    finally:
        session.close()
    assert won is dispatchable
    expected = WorkflowNodeRunStatus.RUNNING if dispatchable else WorkflowNodeRunStatus.PENDING
    assert snapshot(session_factory, _run.id)["nodes"]["eng"] == expected


def test_a_dispatcher_that_read_ready_before_a_cancel_cannot_start_a_branch(
    db, session_factory, bootstrap, tmp_path, monkeypatch
):
    """The race the pre-check cannot close: a dispatcher decided "ready" and
    passed every check, THEN the run was cancelled. Its claim must refuse and
    its half-built TaskRun/AgentRun must be discarded, not committed."""
    built = build_diamond(db, bootstrap.project, label="e8")
    run, node_runs = manual_run(db, built, status=WorkflowRunStatus.CANCELLING)
    complete_node(db, built, node_runs, "eng", text="E", tmp_path=tmp_path)
    before = snapshot(session_factory, run.id)

    monkeypatch.setattr(WorkflowExecutionService, "_accepts_dispatch", lambda self, run_id: True)  # stale check
    session, service = service_on(session_factory)
    try:
        for key in BRANCHES:
            service._dispatch_node_for_execution(session.get(WorkflowNodeRun, node_runs[key].id))
    finally:
        session.close()

    after = snapshot(session_factory, run.id)
    assert {after["nodes"][k] for k in BRANCHES} == {WorkflowNodeRunStatus.PENDING}
    assert delta(before, after) == (0, 0, 0)


def test_a_sibling_stopping_because_of_a_failure_does_not_turn_it_into_a_cancellation(
    db, session_factory, bootstrap, tmp_path
):
    _built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, "e9")
    complete(db, session_factory, run.id, "code", AgentRunStatus.FAILED)
    complete(db, session_factory, run.id, "security", AgentRunStatus.STOPPED)  # obeys the stop request

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["security"] == WorkflowNodeRunStatus.CANCELLED
    assert state["nodes"]["test"] == WorkflowNodeRunStatus.RUNNING  # still draining
    assert state["run"] == WorkflowRunStatus.RUNNING  # NOT CANCELLING: this is a failure, not a cancel
    assert state["nodes"]["join"] == WorkflowNodeRunStatus.PENDING  # not swept into CANCELLED

    complete(db, session_factory, run.id, "test", AgentRunStatus.STOPPED)
    final = snapshot(session_factory, run.id)
    assert final["run"] == WorkflowRunStatus.FAILED
    assert final["nodes"]["join"] == WorkflowNodeRunStatus.PENDING  # "never ran" stays PENDING
    assert "workflow.cancelled" not in event_types(session_factory, _built.task_run.id)


def test_an_agent_stopped_on_its_own_still_cancels_the_run(db, session_factory, bootstrap, tmp_path):
    """Boundary of the rule above (MA7.3 behavior, unchanged): a node's agent
    that stops with NO failure and NO workflow cancellation behind it means the
    run is being cancelled."""
    _built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, "e10")
    complete(db, session_factory, run.id, "security", AgentRunStatus.STOPPED)
    assert run_of(session_factory, run.id) == WorkflowRunStatus.CANCELLING


def test_fan_in_is_never_dispatched_when_a_source_failed(db, session_factory, bootstrap, tmp_path):
    _built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, "e5")
    complete(db, session_factory, run.id, "test", tmp_path=tmp_path)
    complete(db, session_factory, run.id, "security", tmp_path=tmp_path)
    complete(db, session_factory, run.id, "code", AgentRunStatus.FAILED)  # the last source FAILS

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["join"] == WorkflowNodeRunStatus.PENDING
    assert state["run"] == WorkflowRunStatus.FAILED and state["run_ended_at"] is not None  # nothing left to drain
    assert state["nodes"]["test"] == state["nodes"]["security"] == WorkflowNodeRunStatus.COMPLETED  # kept
    assert (state["agent_runs_total"], state["jobs_total"]) == (4, 4)


def test_concurrent_failure_and_completion_settle_on_one_failed_outcome(
    db, session_factory, bootstrap, tmp_path
):
    """One branch fails and another completes at the same instant while the
    third is still running, with reconciliations racing both. The run must
    neither finalize early nor lose the failure; the third branch is asked to
    stop exactly once; when it does, the run finalizes FAILED exactly once."""
    for round_number in range(6):
        built, run = start_fanned_out_diamond(
            db, session_factory, bootstrap.project, tmp_path, f"e6-{round_number}"
        )
        failing = finish(db, session_factory, run.id, "code", AgentRunStatus.FAILED)
        completing = finish(db, session_factory, run.id, "security", tmp_path=tmp_path)
        before = snapshot(session_factory, run.id)

        def report(i, agents=(failing, completing), run_id=run.id):
            session, service = service_on(session_factory)
            try:
                service.on_agent_run_complete(agents[i % 2].id)
                service.reconcile_workflow_run(run_id)
            finally:
                session.close()

        in_threads(6, report)

        mid = snapshot(session_factory, run.id)
        assert mid["run"] == WorkflowRunStatus.RUNNING and mid["run_ended_at"] is None, round_number  # 'test' drains
        assert mid["nodes"]["code"] == WorkflowNodeRunStatus.FAILED
        assert mid["nodes"]["security"] == WorkflowNodeRunStatus.COMPLETED  # kept
        assert mid["nodes"]["test"] == WorkflowNodeRunStatus.RUNNING
        assert agent_of(db, session_factory, run.id, "test").cancellation_requested_at is not None
        assert mid["nodes"]["join"] == WorkflowNodeRunStatus.PENDING
        assert delta(before, mid) == (0, 0, 0)  # nothing dispatched after the failure

        stopped = finish(db, session_factory, run.id, "test", AgentRunStatus.STOPPED)  # it obeys
        in_threads(4, lambda _i, agent_id=stopped.id, run_id=run.id: _report_and_reconcile(session_factory, agent_id, run_id))

        state = snapshot(session_factory, run.id)
        assert state["run"] == WorkflowRunStatus.FAILED and state["run_ended_at"] is not None, round_number
        assert state["nodes"]["test"] == WorkflowNodeRunStatus.CANCELLED
        assert state["nodes"]["join"] == WorkflowNodeRunStatus.PENDING  # downstream of the failure: never ran
        assert delta(before, state) == (0, 0, 0)
        assert count_events(session_factory, built.task_run.id, "workflow.failed") == 1
        assert count_events(session_factory, built.task_run.id, "workflow.node.failed") == 1


def _report_and_reconcile(session_factory, agent_id, run_id):
    session, service = service_on(session_factory)
    try:
        service.on_agent_run_complete(agent_id)
        service.reconcile_workflow_run(run_id)
    finally:
        session.close()


# =============================================================================
# F. Workflow cancellation across parallel branches
# =============================================================================


def test_cancel_signals_every_queued_branch_and_converges_through_the_worker(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    graph = build_acceptance(db, bootstrap.project, label="f1")
    provider = install_provider(monkeypatch, RoleProvider(graph.role_of))
    run = start(db, graph.built)
    solo = new_worker(session_factory)
    solo.run_once()
    solo.run_once()  # three queued branches

    WorkflowExecutionService(db).cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)

    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.CANCELLING
    assert {state["nodes"][k] for k in ("aggregator", "result")} == {WorkflowNodeRunStatus.CANCELLED}
    for key in REVIEWERS:
        assert agent_of(db, session_factory, run.id, key).cancellation_requested_at is not None
    assert state["nodes"]["planner"] == state["nodes"]["engineer"] == WorkflowNodeRunStatus.COMPLETED  # kept

    while solo.run_once():
        pass  # the worker claims the queued jobs; each stops at its first checkpoint

    final = snapshot(session_factory, run.id)
    assert final["run"] == WorkflowRunStatus.CANCELLED and final["run_ended_at"] is not None
    assert {final["nodes"][k] for k in REVIEWERS} == {WorkflowNodeRunStatus.CANCELLED}
    assert provider.roles() == ["planner", "engineer"]  # no branch ever reached the model
    assert final["agent_runs_total"] == 5  # nothing new was dispatched


def test_cancel_with_one_branch_completed_and_two_in_flight(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    graph = build_acceptance(db, bootstrap.project, label="f2")
    provider = install_provider(monkeypatch, RoleProvider(graph.role_of))
    in_flight = {"code_reviewer": threading.Event(), "test_engineer": threading.Event()}
    release = threading.Event()

    def blocked(role):
        def hook(_request):
            in_flight[role].set()
            assert release.wait(30)

        return hook

    provider.hooks.update(code_reviewer=blocked("code_reviewer"), test_engineer=blocked("test_engineer"))
    run = start(db, graph.built)
    solo = new_worker(session_factory)
    solo.run_once()
    solo.run_once()

    with running_workers(session_factory, 2):  # security completes; code + test hold the two workers
        assert wait_until(
            lambda: nodes_of(session_factory, run.id)["security_reviewer"] == WorkflowNodeRunStatus.COMPLETED
            and all(event.is_set() for event in in_flight.values())
        )
        WorkflowExecutionService(db).cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)
        mid = snapshot(session_factory, run.id)
        assert mid["run"] == WorkflowRunStatus.CANCELLING  # two calls still in flight
        assert mid["nodes"]["security_reviewer"] == WorkflowNodeRunStatus.COMPLETED
        assert mid["nodes"]["aggregator"] == WorkflowNodeRunStatus.CANCELLED
        release.set()
        assert wait_until(lambda: run_of(session_factory, run.id) == WorkflowRunStatus.CANCELLED)

    final = snapshot(session_factory, run.id)
    assert final["nodes"]["security_reviewer"] == WorkflowNodeRunStatus.COMPLETED  # completed output preserved
    assert final["node_runs"]["security_reviewer"].output_snapshot_ref is not None
    assert final["nodes"]["code_reviewer"] == final["nodes"]["test_engineer"] == WorkflowNodeRunStatus.CANCELLED
    assert provider.count("aggregator") == 0
    assert final["agent_runs_total"] == 5


def test_cancel_closes_a_waiting_gate_that_is_one_branch_of_a_fan_out(
    db, session_factory, bootstrap, worker, calls
):
    graph = gate_graph(db, bootstrap.project, "f3")
    run = start(db, graph.built)
    worker.run_once()  # entry: a, gate (waiting), z
    approval = snapshot(session_factory, run.id)["approvals"][0]

    WorkflowExecutionService(db).cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)

    state = snapshot(session_factory, run.id)
    assert state["approvals"][0].id == approval.id
    assert state["approvals"][0].status == ApprovalStatus.EXPIRED
    assert state["approvals"][0].resolution_note == "workflow_cancelled"
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.CANCELLED
    assert state["run"] == WorkflowRunStatus.CANCELLING  # a and z are still in flight
    for key in ("a", "z"):
        agent = finish(db, session_factory, run.id, key, AgentRunStatus.STOPPED)
        WorkflowExecutionService(db).on_agent_run_complete(agent.id)
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.CANCELLED


def test_a_cancel_arriving_mid_fan_out_stops_the_rest_of_the_batch(
    db, session_factory, bootstrap, tmp_path, monkeypatch
):
    """Cancelled between the first and second dispatch of one scheduling pass:
    the remaining ready branches are NOT dispatched (the run is re-checked
    before each one), and nothing is left half-created."""
    built = build_diamond(db, bootstrap.project, label="f4")
    run = start(db, built)
    eng = finish(db, session_factory, run.id, "eng", tmp_path=tmp_path)
    real = WorkflowExecutionService._dispatch_node_for_execution
    fired = {"done": False}

    def cancelling_after_first(self, node_run):
        real(self, node_run)
        if not fired["done"]:
            fired["done"] = True
            session, service = service_on(session_factory)
            try:
                service.cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)
            finally:
                session.close()

    monkeypatch.setattr(WorkflowExecutionService, "_dispatch_node_for_execution", cancelling_after_first)
    WorkflowExecutionService(db).on_agent_run_complete(eng.id)

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["code"] == WorkflowNodeRunStatus.RUNNING  # dispatched before the cancel
    assert state["nodes"]["security"] == state["nodes"]["test"] == WorkflowNodeRunStatus.CANCELLED
    assert (state["agent_runs_total"], state["task_runs_total"], state["jobs_total"]) == (2, 3, 2)
    assert state["run"] == WorkflowRunStatus.CANCELLING


# =============================================================================
# G. Recovery / reconciliation
# =============================================================================


def test_partial_fan_out_crash_is_completed_by_reconciliation_without_duplicates(
    db, session_factory, bootstrap, tmp_path, monkeypatch, worker
):
    built = build_diamond(db, bootstrap.project, label="g1")
    run = start(db, built)
    eng = finish(db, session_factory, run.id, "eng", tmp_path=tmp_path)
    real = WorkflowExecutionService._dispatch_node_for_execution
    dispatched = []

    def dies_after_first(self, node_run):
        if dispatched:
            raise RuntimeError("process died mid fan-out")
        dispatched.append(node_run.id)
        return real(self, node_run)

    monkeypatch.setattr(WorkflowExecutionService, "_dispatch_node_for_execution", dies_after_first)
    with pytest.raises(RuntimeError):
        WorkflowExecutionService(db).on_agent_run_complete(eng.id)
    monkeypatch.setattr(WorkflowExecutionService, "_dispatch_node_for_execution", real)

    crashed = snapshot(session_factory, run.id)
    assert crashed["nodes"]["code"] == WorkflowNodeRunStatus.RUNNING  # only ONE branch got out
    assert crashed["nodes"]["security"] == crashed["nodes"]["test"] == WorkflowNodeRunStatus.PENDING
    assert totals(crashed) == (2, 3, 2)

    for _ in range(3):  # repeated recovery converges, never duplicates
        worker.reconcile_workflows()
        state = snapshot(session_factory, run.id)
        assert {state["nodes"][k] for k in BRANCHES} == {WorkflowNodeRunStatus.RUNNING}
        assert totals(state) == (4, 5, 4)
    assert dispatch_order(session_factory, run.id) == ["eng", "code", "security", "test"]


def test_branch_claimed_but_its_job_was_never_enqueued_is_repaired(
    db, session_factory, bootstrap, tmp_path, monkeypatch, worker
):
    built = build_diamond(db, bootstrap.project, label="g2")
    run = start(db, built)
    eng = finish(db, session_factory, run.id, "eng", tmp_path=tmp_path)
    real_enqueue = JobQueueRepository.enqueue
    monkeypatch.setattr(
        JobQueueRepository,
        "enqueue",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("died before enqueue")),
    )
    with pytest.raises(RuntimeError):
        WorkflowExecutionService(db).on_agent_run_complete(eng.id)
    monkeypatch.setattr(JobQueueRepository, "enqueue", real_enqueue)

    stranded = snapshot(session_factory, run.id)
    assert stranded["nodes"]["code"] == WorkflowNodeRunStatus.RUNNING  # claimed, AgentRun exists...
    assert totals(stranded) == (2, 3, 1)  # ...but no job for it

    for _ in range(3):
        worker.reconcile_workflows()
        state = snapshot(session_factory, run.id)
        assert {state["nodes"][k] for k in BRANCHES} == {WorkflowNodeRunStatus.RUNNING}
        assert totals(state) == (4, 5, 4)  # one job per AgentRun, however often it runs


def test_restart_during_partial_completion_resumes_exactly_where_it_stopped(
    db, session_factory, bootstrap, tmp_path, worker
):
    _built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, "g3")
    finish(db, session_factory, run.id, "security", tmp_path=tmp_path)  # finished; callback lost in a crash

    for _ in range(2):  # the restarted worker's startup sweep
        worker.reconcile_workflows()
    state = snapshot(session_factory, run.id)
    assert state["nodes"]["security"] == WorkflowNodeRunStatus.COMPLETED
    assert state["nodes"]["code"] == state["nodes"]["test"] == WorkflowNodeRunStatus.RUNNING
    assert state["nodes"]["join"] == WorkflowNodeRunStatus.PENDING
    assert totals(state) == (4, 5, 4)

    finish(db, session_factory, run.id, "code", tmp_path=tmp_path)
    finish(db, session_factory, run.id, "test", tmp_path=tmp_path)
    for _ in range(3):
        worker.reconcile_workflows()
    state = snapshot(session_factory, run.id)
    assert state["nodes"]["join"] == WorkflowNodeRunStatus.RUNNING
    assert totals(state) == (5, 6, 5)


def test_concurrent_reconciliation_of_a_partially_dispatched_fan_out(
    db, session_factory, bootstrap, tmp_path, monkeypatch
):
    real = WorkflowExecutionService._dispatch_node_for_execution
    for round_number in range(6):
        built = build_diamond(db, bootstrap.project, label=f"g5-{round_number}")
        run = start(db, built)
        eng = finish(db, session_factory, run.id, "eng", tmp_path=tmp_path)
        monkeypatch.setattr(WorkflowExecutionService, "_dispatch_node_for_execution", lambda self, nr: None)
        WorkflowExecutionService(db).on_agent_run_complete(eng.id)  # completed, nothing dispatched
        monkeypatch.setattr(WorkflowExecutionService, "_dispatch_node_for_execution", real)
        before = snapshot(session_factory, run.id)
        assert before["nodes"]["eng"] == WorkflowNodeRunStatus.COMPLETED
        assert {before["nodes"][k] for k in BRANCHES} == {WorkflowNodeRunStatus.PENDING}

        in_threads(5, lambda _i: new_worker(session_factory).reconcile_workflows())

        state = snapshot(session_factory, run.id)
        assert {state["nodes"][k] for k in BRANCHES} == {WorkflowNodeRunStatus.RUNNING}, round_number
        assert delta(before, state) == (3, 3, 3), round_number
        # racing dispatchers may enqueue in any interleaving; what must hold is exactly-once
        assert sorted(dispatch_order(session_factory, run.id)) == ["code", "eng", "security", "test"]
        assert count_events(session_factory, built.task_run.id, "workflow.node.scheduled") == 4


def test_interrupted_failure_propagation_is_completed_by_reconciliation(
    db, session_factory, bootstrap, tmp_path, monkeypatch, worker
):
    graph = gate_graph(db, bootstrap.project, "g6")
    run = start(db, graph.built)
    # agents are never executed here; state is driven by hand: entry -> a, gate (waiting), z
    complete(db, session_factory, run.id, "entry", tmp_path=tmp_path)
    assert snapshot(session_factory, run.id)["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL

    real_propagate = WorkflowExecutionService._propagate_failure
    monkeypatch.setattr(WorkflowExecutionService, "_propagate_failure", lambda self, run_id: None)  # dies here
    complete(db, session_factory, run.id, "a", AgentRunStatus.FAILED)
    monkeypatch.setattr(WorkflowExecutionService, "_propagate_failure", real_propagate)
    crashed = snapshot(session_factory, run.id)
    assert crashed["nodes"]["a"] == WorkflowNodeRunStatus.FAILED
    assert crashed["nodes"]["z"] == WorkflowNodeRunStatus.RUNNING  # never told to stop
    assert crashed["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL  # still open
    assert agent_of(db, session_factory, run.id, "z").cancellation_requested_at is None

    for _ in range(3):
        worker.reconcile_workflows()
    state = snapshot(session_factory, run.id)
    assert agent_of(db, session_factory, run.id, "z").cancellation_requested_at is not None
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.CANCELLED
    assert state["approvals"][0].status == ApprovalStatus.EXPIRED
    assert state["approvals"][0].resolution_note == "workflow_failed"
    assert state["run"] == WorkflowRunStatus.RUNNING  # z is still draining

    first_request = agent_of(db, session_factory, run.id, "z").cancellation_requested_at
    worker.reconcile_workflows()
    assert agent_of(db, session_factory, run.id, "z").cancellation_requested_at == first_request  # not re-stamped

    complete(db, session_factory, run.id, "z", AgentRunStatus.STOPPED)
    assert run_of(session_factory, run.id) == WorkflowRunStatus.FAILED
    assert count_events(session_factory, graph.built.task_run.id, "workflow.failed") == 1


def test_interrupted_cancellation_propagation_is_completed_by_reconciliation(
    db, session_factory, bootstrap, tmp_path, monkeypatch, worker
):
    _built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, "g7")
    real_signal = WorkflowExecutionService._signal_active_agents
    monkeypatch.setattr(
        WorkflowExecutionService,
        "_signal_active_agents",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("died before signalling")),
    )
    with pytest.raises(RuntimeError):
        WorkflowExecutionService(db).cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)
    monkeypatch.setattr(WorkflowExecutionService, "_signal_active_agents", real_signal)

    crashed = snapshot(session_factory, run.id)
    assert crashed["run"] == WorkflowRunStatus.CANCELLING
    assert crashed["nodes"]["join"] == WorkflowNodeRunStatus.CANCELLED  # pending nodes already cancelled
    assert all(agent_of(db, session_factory, run.id, k).cancellation_requested_at is None for k in BRANCHES)

    for _ in range(2):
        worker.reconcile_workflows()
    for key in BRANCHES:
        assert agent_of(db, session_factory, run.id, key).cancellation_requested_at is not None
    requested_by = {agent_of(db, session_factory, run.id, k).cancellation_requested_by for k in BRANCHES}
    assert requested_by == {bootstrap.user.id}  # the ORIGINAL requester, not the sweep

    for key in BRANCHES:
        agent = finish(db, session_factory, run.id, key, AgentRunStatus.STOPPED)
        WorkflowExecutionService(db).on_agent_run_complete(agent.id)
    assert run_of(session_factory, run.id) == WorkflowRunStatus.CANCELLED


def test_repeated_reconciliation_of_a_settled_parallel_run_is_a_no_op(
    db, session_factory, bootstrap, tmp_path, worker
):
    built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, "g8")
    complete(db, session_factory, run.id, "test", tmp_path=tmp_path)
    before = snapshot(session_factory, run.id)
    types_before = event_types(session_factory, built.task_run.id)

    for _ in range(5):
        assert worker.reconcile_workflows() == 1

    after = snapshot(session_factory, run.id)
    assert after["nodes"] == before["nodes"] and after["run"] == before["run"]
    assert totals(after) == totals(before)
    assert event_types(session_factory, built.task_run.id) == types_before


# =============================================================================
# H. Multi-parent Agent context
# =============================================================================


def test_fan_in_agent_context_is_labelled_canonical_and_complete(db, session_factory, bootstrap, tmp_path):
    _built, run = start_fanned_out_diamond(db, session_factory, bootstrap.project, tmp_path, "h1")
    for key in ("test", "code", "security"):  # completion order != canonical order
        complete(db, session_factory, run.id, key, tmp_path=tmp_path)
    state = snapshot(session_factory, run.id)
    context = db.get(AgentRun, state["node_runs"]["join"].agent_run_id).input_context_json

    assert context["kind"] == "workflow_upstream"
    assert [entry["node_key"] for entry in context["upstream"]] == ["code", "security", "test"]
    assert context["upstream_artifact_ids"] == [state["node_runs"][k].output_snapshot_ref for k in ("code", "security", "test")]
    assert context["upstream_node_run_ids"] == [state["node_runs"][k].id for k in ("code", "security", "test")]


def test_a_tampered_branch_output_fails_the_aggregator_before_any_provider_call(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    graph = build_acceptance(db, bootstrap.project, label="h2")
    provider = install_provider(monkeypatch, RoleProvider(graph.role_of))
    tamper = threading.Event()
    proceed = threading.Event()

    def gated(_request):
        tamper.set()
        assert proceed.wait(30)

    provider.hooks["code_reviewer"] = gated
    run = start(db, graph.built)
    solo = new_worker(session_factory)
    solo.run_once()
    solo.run_once()

    with running_workers(session_factory, 3):
        assert wait_until(
            lambda: tamper.is_set()
            and nodes_of(session_factory, run.id)["security_reviewer"] == WorkflowNodeRunStatus.COMPLETED
            and nodes_of(session_factory, run.id)["test_engineer"] == WorkflowNodeRunStatus.COMPLETED
        )
        victim = snapshot(session_factory, run.id)["node_runs"]["security_reviewer"].output_snapshot_ref
        check = session_factory()
        try:
            Path(check.get(Artifact, victim).storage_ref).write_text("altered after completion", encoding="utf-8")
        finally:
            check.close()
        proceed.set()
        assert wait_until(lambda: run_of(session_factory, run.id) == WorkflowRunStatus.FAILED)

    final = snapshot(session_factory, run.id)
    assert final["nodes"]["aggregator"] == WorkflowNodeRunStatus.FAILED
    assert provider.count("aggregator") == 0  # fail closed BEFORE inference
    aggregator_agent = db.get(AgentRun, final["node_runs"]["aggregator"].agent_run_id)
    db.refresh(aggregator_agent)
    assert aggregator_agent.status == AgentRunStatus.FAILED


# =============================================================================
# I. Maximum fan-out, conditional edges, unsupported node types
# =============================================================================


def test_default_max_fan_out_and_its_bounds():
    assert Settings().workflow_max_fan_out == 8
    assert Settings(workflow_max_fan_out=1).workflow_max_fan_out == 1
    assert Settings(workflow_max_fan_out=64).workflow_max_fan_out == 64
    for bad in (0, -1, 65):
        with pytest.raises(ValueError):
            Settings(workflow_max_fan_out=bad)


def test_max_fan_out_boundary_is_accepted(db, bootstrap):
    graph = build_star(db, bootstrap.project, settings.workflow_max_fan_out, label="i1")  # exactly at the limit
    assert graph.built.published.status == VersionStatus.ACTIVE


def test_exceeding_the_max_fan_out_is_rejected_at_publish(db, bootstrap):
    limit = settings.workflow_max_fan_out
    with pytest.raises(DAGValidationError) as exc:
        build_star(db, bootstrap.project, limit + 1, label="i2")
    assert any(
        "entry" in issue and f"fans out to {limit + 1}" in issue and f"maximum is {limit}" in issue
        for issue in exc.value.issues
    )


def test_the_limit_is_configurable_and_counts_direct_branches_only(db, bootstrap, monkeypatch):
    monkeypatch.setattr(settings, "workflow_max_fan_out", 3)
    build_star(db, bootstrap.project, 3, label="i3")  # fine
    with pytest.raises(DAGValidationError):
        build_star(db, bootstrap.project, 4, label="i3b")
    # depth is not width: a long chain of fan-outs of 2 is fine
    nodes = [(f"n{i}", AGENT) for i in range(6)] + [("end", TERMINAL)]
    edges = [(f"n{i}", f"n{i + 1}") for i in range(5)] + [("n5", "end")] + [(f"n{i}", "end") for i in range(5)]
    build_graph(db, bootstrap.project, nodes, edges, label="i3c")


def test_fan_in_width_is_not_limited_by_the_fan_out_cap(db, bootstrap, monkeypatch):
    monkeypatch.setattr(settings, "workflow_max_fan_out", 2)
    nodes = [("a", AGENT), ("b", AGENT), ("c", AGENT), ("d", AGENT), ("join", AGENT), ("end", TERMINAL)]
    edges = [("a", "b"), ("a", "c"), ("b", "join"), ("c", "join"), ("d", "join"), ("join", "end")]
    build_graph(db, bootstrap.project, nodes, edges, label="i4")  # 'join' has 3 inputs; nobody fans out > 2


def test_start_refuses_a_version_that_bypassed_the_publish_time_cap(db, bootstrap, monkeypatch):
    graph = build_star(db, bootstrap.project, 5, label="i5")  # published under the default limit of 8
    monkeypatch.setattr(settings, "workflow_max_fan_out", 4)  # the operator later tightened it
    with pytest.raises(WorkflowExecutionError, match="maximum is 4"):
        WorkflowExecutionService(db).start_workflow_run(graph.built.published.id, graph.built.task_run.id)
    assert db.query(WorkflowRun).filter(WorkflowRun.workflow_version_id == graph.built.published.id).count() == 0


def test_conditional_edges_are_still_rejected_in_a_fan_out(db, bootstrap):
    built = build_diamond(db, bootstrap.project, label="i6", condition_on="security")  # left unpublished
    with pytest.raises(DAGValidationError) as exc:
        WorkflowDefinitionService(db).publish_version(built.workflow.id, built.version.version)
    assert any("conditional routing is not supported" in issue for issue in exc.value.issues)

    built.version.status = VersionStatus.ACTIVE  # activated without validation: still refused at start
    db.commit()
    with pytest.raises(WorkflowExecutionError, match="conditional"):
        WorkflowExecutionService(db).start_workflow_run(built.version.id, built.task_run.id)


@pytest.mark.parametrize(
    "node_type",
    [
        WorkflowNodeType.JUDGE,
        WorkflowNodeType.CONSENSUS,
        WorkflowNodeType.CONDITIONAL,
        WorkflowNodeType.PARALLEL_GROUP,
        WorkflowNodeType.REPAIR_LOOP,
    ],
)
def test_unsupported_node_types_still_fail_closed_and_are_not_repurposed_as_parallelism(
    db, session_factory, bootstrap, node_type
):
    version_id, task_run_id = single_unsupported_node(
        db, bootstrap.project, f"i7-{node_type.value}", node_type=node_type, max_iterations=2
    )
    run = WorkflowExecutionService(db).start_workflow_run(version_id, task_run_id)
    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.FAILED and state["run_ended_at"] is not None
    assert state["nodes"]["j"] == WorkflowNodeRunStatus.FAILED
    assert (state["agent_runs_total"], state["jobs_total"]) == (0, 0)


def test_an_unsupported_branch_fails_the_run_fast_and_stops_its_siblings(
    db, session_factory, bootstrap, tmp_path
):
    """entry -> {a_agent, judge}: the JUDGE branch cannot run, so the whole run
    fails fast and the agent branch that DID start is asked to stop."""
    definitions = WorkflowDefinitionService(db)
    workflow = definitions.create_workflow(bootstrap.project.id, "i8")
    version = definitions.get_latest_version(workflow.id)
    made = {}
    for key in ("entry", "a_agent"):
        made[key] = definitions.add_node(
            workflow.id,
            version.version,
            key,
            WorkflowNodeType.AGENT,
            config={"agent_version_id": make_agent_version(db, make_agent(db, bootstrap.project, key)).id},
        )
    made["judge"] = definitions.add_node(workflow.id, version.version, "judge", WorkflowNodeType.JUDGE, config={})
    definitions.add_edge(workflow.id, version.version, made["entry"].id, made["a_agent"].id)
    definitions.add_edge(workflow.id, version.version, made["entry"].id, made["judge"].id)
    version.status = VersionStatus.ACTIVE
    task_run = TaskRun(task_id=make_task(db, bootstrap.project).id, status=TaskRunStatus.CREATED)
    db.add(task_run)
    db.commit()
    run = WorkflowExecutionService(db).start_workflow_run(version.id, task_run.id)
    complete(db, session_factory, run.id, "entry", tmp_path=tmp_path)

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["judge"] == WorkflowNodeRunStatus.FAILED
    assert agent_of(db, session_factory, run.id, "a_agent").cancellation_requested_at is not None
    assert state["run"] == WorkflowRunStatus.RUNNING  # a_agent still draining
    complete(db, session_factory, run.id, "a_agent", AgentRunStatus.STOPPED)
    assert run_of(session_factory, run.id) == WorkflowRunStatus.FAILED


# =============================================================================
# J. Sequential behavior unchanged
# =============================================================================


def workflow_events(session_factory, task_run_id):
    return [t for t in event_types(session_factory, task_run_id) if t.startswith("workflow.")]


def test_a_sequential_workflow_records_exactly_the_same_evidence_as_before(
    db, session_factory, bootstrap, worker, calls
):
    built = build_chain(
        db, bootstrap.project, [("first", AGENT), ("second", AGENT), ("result", TERMINAL)], label="j1"
    )
    run = start(db, built)
    drain(worker)

    assert calls == ["first", "second"]
    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.COMPLETED and state["run_ended_at"] is not None
    assert workflow_events(session_factory, built.task_run.id) == [
        "workflow.started",
        "workflow.node.scheduled",
        "workflow.node.completed",
        "workflow.node.scheduled",
        "workflow.node.completed",
        "workflow.node.completed",  # TERMINAL
        "workflow.completed",
    ]


def test_a_sequential_agent_failure_finalizes_the_run_failed_with_ended_at(
    db, session_factory, bootstrap, worker, monkeypatch, tmp_path
):
    import app.services.execution_service as execution_service_module

    def failing(self, agent_run_id, *, worker_id):
        agent_run = self.db.get(AgentRun, agent_run_id)
        agent_run.status = AgentRunStatus.FAILED
        self.db.commit()

    monkeypatch.setattr(execution_service_module.AgentExecutionService, "execute", failing)
    built = build_chain(
        db, bootstrap.project, [("first", AGENT), ("second", AGENT), ("result", TERMINAL)], label="j2"
    )
    run = start(db, built)
    drain(worker)

    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.FAILED and state["run_ended_at"] is not None
    assert state["nodes"]["first"] == WorkflowNodeRunStatus.FAILED
    assert state["nodes"]["second"] == state["nodes"]["result"] == WorkflowNodeRunStatus.PENDING  # never ran
    assert state["agent_runs_total"] == 1
    assert workflow_events(session_factory, built.task_run.id) == [
        "workflow.started",
        "workflow.node.scheduled",
        "workflow.node.failed",
        "workflow.failed",
    ]


def test_a_sequential_workflow_never_has_more_than_one_node_in_flight(
    db, session_factory, bootstrap, worker, calls
):
    built = build_chain(
        db, bootstrap.project, [("one", AGENT), ("two", AGENT), ("three", AGENT), ("end", TERMINAL)], label="j3"
    )
    run = start(db, built)
    while worker.run_once():
        running = [k for k, v in nodes_of(session_factory, run.id).items() if v == WorkflowNodeRunStatus.RUNNING]
        assert len(running) <= 1
    assert calls == ["one", "two", "three"]
    assert run_of(session_factory, run.id) == WorkflowRunStatus.COMPLETED

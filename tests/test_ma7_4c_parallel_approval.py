"""MA7.4c -- parallel Human Approval, Worker concurrency, upstream-context cap.

A. FINAL MA7.4 ACCEPTANCE: Planner -> Engineer -> {Test, Security, Code} ->
   Human Approval (the ALL-of barrier itself) -> TERMINAL, through the real
   WorkflowExecutionService, ONE real ``Worker(concurrency=3)``, the real
   durable queue and TaskRun/AgentRun lifecycle. Only the provider is faked.
   Approve path and Reject path.
B. Evidence: the fingerprint binds all outputs (with provenance); the evidence
   API; any changed/replaced/deleted/tampered output fails approval closed;
   authorization and cross-project isolation of the API.
C. Multi-parent gate mechanics: created once, only after ALL sources, no
   execution artifacts; downstream nodes receive every approved output.
D. Recovery / reconciliation / concurrency around the gate.
E. Worker concurrency (config, CLI, lanes, shutdown, failure).
F. Upstream-context cap (fail closed, before inference, never truncated).

Every database is a disposable temp file; nothing here opens data/*.db and
nothing writes to the real artifacts/logs directories.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import text

import app.logging_config as logging_config_module
import app.worker as worker_module
from app.config import Settings, settings
from app.db.enums import (
    AgentRunStatus,
    ApprovalStatus,
    JobQueueStatus,
    JobType,
    ProjectRole,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
    WorkflowRunStatus,
)
from app.errors import InvalidStateTransitionError
from app.models.artifacts_eval import Artifact
from app.models.execution import JobQueue
from app.models.identity import Project, ProjectMembership
from app.models.providers import Model
from app.models.tasks import AgentRun, AgentRunAttempt
from app.models.workflow import WorkflowNodeRun
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.approval_service import (
    WORKFLOW_HUMAN_APPROVAL_OPERATION,
    ApprovalService,
    compute_action_fingerprint,
)
from app.services.execution_service import AgentExecutionService, _WorkflowContextTooLarge
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.workflow_execution_service import WorkflowExecutionService
from app.services.workflow_validation_service import DAGValidationError
from app.worker import Worker, parse_args
from tests.conftest import make_agent, make_runnable_agent_version
from tests.ma7_3b_support import (
    AGENT,
    APPROVAL,
    TERMINAL,
    drain,
    event_types,
    install_fake_agents,
    resolve_via_api,
    snapshot,
    start,
)
from tests.ma7_4a_support import finish_agent
from tests.ma7_4b_support import (
    GATE_ACCEPTANCE_EDGES,
    GATE_ACCEPTANCE_NODES,
    REVIEWERS,
    RoleProvider,
    build_acceptance,
    build_gate_acceptance,
    build_graph,
    install_provider,
    new_worker,
    running_worker,
    wait_until,
)


@pytest.fixture()
def calls(monkeypatch, tmp_path):
    return install_fake_agents(monkeypatch, tmp_path)


@pytest.fixture()
def worker(session_factory):
    return new_worker(session_factory)


@pytest.fixture()
def artifacts_dir(monkeypatch, tmp_path):
    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


# -- helpers ---------------------------------------------------------------------------


def nodes_of(session_factory, run_id):
    return snapshot(session_factory, run_id)["nodes"]


def run_of(session_factory, run_id):
    return snapshot(session_factory, run_id)["run"]


def totals(state):
    return (state["agent_runs_total"], state["task_runs_total"], state["jobs_total"])


def delta(before, after):
    return tuple(a - b for a, b in zip(totals(after), totals(before)))


def agent_of(db, session_factory, run_id, key):
    agent = db.get(AgentRun, snapshot(session_factory, run_id)["node_runs"][key].agent_run_id)
    db.refresh(agent)
    return agent


def finish(db, session_factory, run_id, key, status=AgentRunStatus.COMPLETED, *, tmp_path=None):
    agent = agent_of(db, session_factory, run_id, key)
    finish_agent(db, agent, status, text=f"OUT-{key}", tmp_path=tmp_path)
    return agent


def complete(db, session_factory, run_id, key, status=AgentRunStatus.COMPLETED, *, tmp_path=None):
    agent = finish(db, session_factory, run_id, key, status, tmp_path=tmp_path)
    WorkflowExecutionService(db).on_agent_run_complete(agent.id)
    return agent


def count_events(session_factory, task_run_id, event_type, node_run_id=None):
    from tests.ma7_3b_support import events

    return len(
        [e for e in events(session_factory, task_run_id) if e[0] == event_type and node_run_id in (None, e[3])]
    )


def rows(session_factory, model, *criteria):
    session = session_factory()
    try:
        return session.query(model).filter(*criteria).all()
    finally:
        session.close()


def in_threads(count, work, *, timeout=60):
    barrier = threading.Barrier(count, timeout=20)

    def wrapped(i):
        barrier.wait()
        return work(i)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(wrapped, i) for i in range(count)]
        return [future.result(timeout=timeout) for future in futures]


def stub_gate_graph(db, project, label, *, nodes=None, edges=None):
    """The acceptance shape with STUBBED inference (fast orchestration tests)."""
    return build_graph(
        db,
        project,
        nodes or GATE_ACCEPTANCE_NODES,
        edges or GATE_ACCEPTANCE_EDGES,
        label=label,
        real=False,
    )


def waiting_gate(db, session_factory, project, worker, label):
    """Stub agents all executed by the real Worker; the gate is WAITING."""
    graph = stub_gate_graph(db, project, label)
    run = start(db, graph.built)
    drain(worker)
    state = snapshot(session_factory, run.id)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    return graph, run, state["approvals"][0]


def expected_gate_fingerprint(session_factory, built, run_id, gate_key="gate"):
    """An INDEPENDENT recomputation of the v2 payload from raw rows -- so the
    test pins the documented contract, not the engine's own helper."""
    state = snapshot(session_factory, run_id)
    session = session_factory()
    try:
        upstream = [
            {
                "node_key": key,
                "node_run_id": state["node_runs"][key].id,
                "artifact_id": state["node_runs"][key].output_snapshot_ref,
                "artifact_content_hash": session.get(Artifact, state["node_runs"][key].output_snapshot_ref).content_hash,
            }
            for key in REVIEWERS
        ]
    finally:
        session.close()
    payload = {
        "kind": WORKFLOW_HUMAN_APPROVAL_OPERATION,
        "workflow_run_id": run_id,
        "workflow_node_run_id": state["node_runs"][gate_key].id,
        "workflow_node_id": built.nodes[gate_key].id,
        "node_key": gate_key,
        "iteration": 0,
        "approval_group": "eng-leads",
        "upstream": upstream,
        "evidence_version": 2,
    }
    return compute_action_fingerprint(payload), upstream


def get_evidence(client, headers, approval_id):
    return client.get(f"/approvals/{approval_id}/evidence", headers=headers)


# =============================================================================
# A. FINAL MA7.4 ACCEPTANCE -- real Worker(concurrency=3)
# =============================================================================


def setup_gate_acceptance(db, session_factory, bootstrap, monkeypatch, artifacts_dir, label):
    """Builds the real-inference acceptance workflow and the fake provider,
    with hooks that (a) make the three reviewers meet at a barrier -- proof
    they are in flight simultaneously -- and (b) hold the code reviewer until
    released. Returns ``(graph, provider, release_code, errors)``."""
    graph = build_gate_acceptance(db, bootstrap.project, label=label)
    provider = install_provider(monkeypatch, RoleProvider(graph.role_of))
    meeting = threading.Barrier(3, timeout=25)
    release_code = threading.Event()
    errors = []

    def meet(_request):
        try:
            meeting.wait()
        except Exception as exc:  # only on a regression: the reviewers were NOT concurrent
            errors.append(exc)
            raise

    def code_reviewer(request):
        meet(request)
        assert release_code.wait(30)

    provider.hooks.update(test_engineer=meet, security_reviewer=meet, code_reviewer=code_reviewer)
    return graph, provider, release_code, errors


def test_final_acceptance_approve_path_under_worker_concurrency_3(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    graph, provider, release_code, errors = setup_gate_acceptance(
        db, session_factory, bootstrap, monkeypatch, artifacts_dir, "fa"
    )
    run = start(db, graph.built)
    parent = graph.built.task_run.id

    with running_worker(session_factory, 3) as the_worker:
        # the three branch Agents are IN FLIGHT AT THE SAME TIME (the barrier in the provider
        # only opens if all three are inside it at once -- queued-together is not enough) ...
        assert wait_until(lambda: nodes_of(session_factory, run.id)["engineer"] == WorkflowNodeRunStatus.COMPLETED)
        assert wait_until(
            lambda: {nodes_of(session_factory, run.id)[k] for k in ("test_engineer", "security_reviewer")}
            == {WorkflowNodeRunStatus.COMPLETED}
        )
        mid = snapshot(session_factory, run.id)
        # ...code is still held: Test and Security did not wait for it, and the gate is NOT here yet
        assert mid["nodes"]["code_reviewer"] == WorkflowNodeRunStatus.RUNNING
        assert mid["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING
        assert mid["approvals"] == []
        assert mid["node_runs"]["gate"].agent_run_id is None
        assert mid["run"] == WorkflowRunStatus.RUNNING

        release_code.set()
        assert wait_until(lambda: run_of(session_factory, run.id) == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL)
        assert wait_until(lambda: {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE})

        state = snapshot(session_factory, run.id)
        assert errors == []
        # every agent node executed exactly once
        assert sorted(provider.roles()) == sorted(
            ["planner", "engineer", "test_engineer", "security_reviewer", "code_reviewer"]
        )
        assert provider.roles()[:2] == ["planner", "engineer"]
        # the gate created NO execution artifacts at all
        assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
        assert state["node_runs"]["gate"].agent_run_id is None
        assert totals(state) == (5, 6, 5)  # 5 AgentRuns; 5 node TaskRuns + the parent; 5 jobs
        assert "gate" not in provider.roles()
        # three DIFFERENT lanes of the one worker ran the reviewers
        reviewer_attempts = rows(
            session_factory,
            AgentRunAttempt,
            AgentRunAttempt.agent_run_id.in_([state["node_runs"][k].agent_run_id for k in REVIEWERS]),
        )
        lanes = {a.worker_id for a in reviewer_attempts}
        assert len(lanes) == 3 and all(lane.startswith(the_worker.worker_id + ":lane") for lane in lanes)

        # exactly ONE durable Approval, PENDING, bound to ALL three outputs
        assert len(state["approvals"]) == 1
        approval = state["approvals"][0]
        assert approval.status == ApprovalStatus.PENDING and approval.requested_by is None
        fingerprint, upstream = expected_gate_fingerprint(session_factory, graph.built, run.id)
        assert approval.action_fingerprint == fingerprint
        assert approval.bound_artifact_id == upstream[0]["artifact_id"]
        assert count_events(session_factory, parent, "approval.requested") == 1
        assert count_events(session_factory, parent, "workflow.node.waiting_for_approval") == 1

        # the evidence API exposes all three canonical references (no artifact content)
        response = get_evidence(client, auth_headers, approval.id)
        assert response.status_code == 200
        body = response.json()
        assert body["evidence_version"] == 2 and body["fingerprint_matches"] is True
        assert body["action_fingerprint"] == approval.action_fingerprint
        assert [e["node_key"] for e in body["evidence"]] == list(REVIEWERS)  # node_key ASC
        for entry, bound in zip(body["evidence"], upstream):
            assert entry["node_run_id"] == bound["node_run_id"]
            assert entry["artifact_id"] == bound["artifact_id"]
            assert entry["content_hash"] == bound["artifact_content_hash"]
            assert entry["content_intact"] is True
            assert set(entry) == {"node_key", "node_run_id", "artifact_id", "content_hash", "content_intact"}

        # the human approves: the workflow resumes exactly once and completes
        assert resolve_via_api(client, auth_headers, approval).status_code == 200
        assert wait_until(lambda: run_of(session_factory, run.id) == WorkflowRunStatus.COMPLETED)

    final = snapshot(session_factory, run.id)
    assert final["run_ended_at"] is not None
    assert set(final["nodes"].values()) == {WorkflowNodeRunStatus.COMPLETED}  # incl. gate and TERMINAL
    assert final["approvals"][0].status == ApprovalStatus.APPROVED
    assert totals(final) == (5, 6, 5)  # resuming created nothing new
    assert sorted(provider.roles()) == sorted(
        ["planner", "engineer", "test_engineer", "security_reviewer", "code_reviewer"]
    )
    assert count_events(session_factory, parent, "workflow.completed") == 1
    assert count_events(session_factory, parent, "approval.approved") == 1

    # restart / reconciliation (a brand-new worker, repeated and concurrent) creates no duplicates
    before_events = event_types(session_factory, parent)
    for _ in range(3):
        new_worker(session_factory).reconcile_workflows()
    in_threads(4, lambda _i: new_worker(session_factory).reconcile_workflows())
    after = snapshot(session_factory, run.id)
    assert totals(after) == totals(final) and after["run"] == WorkflowRunStatus.COMPLETED
    assert len(after["approvals"]) == 1
    assert event_types(session_factory, parent) == before_events


def test_final_acceptance_reject_path_with_the_same_fan_in_architecture(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    graph, provider, release_code, errors = setup_gate_acceptance(
        db, session_factory, bootstrap, monkeypatch, artifacts_dir, "fr"
    )
    run = start(db, graph.built)
    parent = graph.built.task_run.id

    with running_worker(session_factory, 3):
        release_code.set()
        assert wait_until(lambda: run_of(session_factory, run.id) == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL)
        assert wait_until(lambda: {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE})
        waiting = snapshot(session_factory, run.id)
        approval = waiting["approvals"][0]
        outputs = {k: waiting["node_runs"][k].output_snapshot_ref for k in REVIEWERS}

        assert resolve_via_api(client, auth_headers, approval, approve=False, notes="not good enough").status_code == 200
        assert wait_until(lambda: run_of(session_factory, run.id) == WorkflowRunStatus.FAILED)

    assert errors == []
    final = snapshot(session_factory, run.id)
    assert final["run_ended_at"] is not None
    assert final["nodes"]["gate"] == WorkflowNodeRunStatus.FAILED
    assert final["nodes"]["result"] == WorkflowNodeRunStatus.PENDING  # nothing downstream ran
    assert final["approvals"][0].status == ApprovalStatus.REJECTED
    # every parallel output is preserved
    for key in REVIEWERS:
        assert final["nodes"][key] == WorkflowNodeRunStatus.COMPLETED
        assert final["node_runs"][key].output_snapshot_ref == outputs[key]
    assert {a.status for a in rows(session_factory, AgentRun)} == {AgentRunStatus.COMPLETED}
    assert totals(final) == totals(waiting)  # the rejection executed nothing
    assert len(provider.roles()) == 5
    assert count_events(session_factory, parent, "workflow.failed") == 1
    assert count_events(session_factory, parent, "approval.rejected") == 1
    assert "workflow.completed" not in event_types(session_factory, parent)
    # a replayed rejection is idempotent; the opposite decision conflicts
    assert resolve_via_api(client, auth_headers, approval, approve=False).status_code == 200
    assert resolve_via_api(client, auth_headers, approval, approve=True).status_code == 409
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.FAILED


# =============================================================================
# B. Evidence: fingerprint, API, tamper detection, authorization
# =============================================================================


def test_the_fingerprint_binds_every_output_with_full_provenance(db, session_factory, bootstrap, worker, calls):
    graph, run, approval = waiting_gate(db, session_factory, bootstrap.project, worker, "b1")
    fingerprint, upstream = expected_gate_fingerprint(session_factory, graph.built, run.id)
    assert approval.action_fingerprint == fingerprint
    assert [u["node_key"] for u in upstream] == list(REVIEWERS)
    assert all(u["artifact_content_hash"] and u["artifact_id"] and u["node_run_id"] for u in upstream)
    # dropping ANY one entry, or reordering, would be a different action
    for drop in range(3):
        _, full = expected_gate_fingerprint(session_factory, graph.built, run.id)
        assert full != [u for i, u in enumerate(full) if i != drop]
    assert (calls.count("gate"), len(calls)) == (0, 5)


def test_a_single_parent_gate_keeps_the_ma73_payload_and_reports_evidence_version_1(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    """Existing PENDING approvals must keep verifying: the single-parent payload
    is byte-identical (the exact-payload pin lives in test_ma7_4a_...)."""
    graph = build_graph(
        db,
        bootstrap.project,
        [("planner", AGENT), ("gate", APPROVAL), ("engineer", AGENT), ("result", TERMINAL)],
        [("planner", "gate"), ("gate", "engineer"), ("engineer", "result")],
        label="b2",
    )
    run = start(db, graph.built)
    drain(worker)
    approval = snapshot(session_factory, run.id)["approvals"][0]
    body = get_evidence(client, auth_headers, approval.id).json()
    assert body["evidence_version"] == 1 and body["fingerprint_matches"] is True
    assert [e["node_key"] for e in body["evidence"]] == ["planner"]
    assert resolve_via_api(client, auth_headers, approval).status_code == 200
    drain(worker)
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED


@pytest.mark.parametrize("tamper", ["edit_file", "delete_file", "delete_row", "swap_ref", "alter_hash"])
def test_any_change_replacement_deletion_or_tampering_fails_approval_closed(
    client, db, session_factory, auth_headers, bootstrap, worker, calls, tamper
):
    _graph, run, approval = waiting_gate(db, session_factory, bootstrap.project, worker, f"b3-{tamper}")
    state = snapshot(session_factory, run.id)
    victim = state["node_runs"]["security_reviewer"]  # the MIDDLE source
    other = state["node_runs"]["test_engineer"]
    artifact = db.get(Artifact, victim.output_snapshot_ref)
    db.refresh(artifact)
    path, original_bytes, original_hash = Path(artifact.storage_ref), Path(artifact.storage_ref).read_bytes(), artifact.content_hash
    before = snapshot(session_factory, run.id)

    if tamper == "edit_file":
        path.write_text("changed after it was approved", encoding="utf-8")
    elif tamper == "delete_file":
        path.unlink()
    elif tamper == "delete_row":
        db.execute(text("DELETE FROM artifacts WHERE id = :id"), {"id": artifact.id})
        db.commit()
    elif tamper == "swap_ref":
        db.execute(
            text("UPDATE workflow_node_runs SET output_snapshot_ref = :ref WHERE id = :id"),
            {"ref": other.output_snapshot_ref, "id": victim.id},
        )
        db.commit()
    else:
        db.execute(text("UPDATE artifacts SET content_hash = :h WHERE id = :id"), {"h": "e" * 64, "id": artifact.id})
        db.commit()

    body = get_evidence(client, auth_headers, approval.id).json()
    assert body["fingerprint_matches"] is False  # the API says so ...
    by_key = {e["node_key"]: e for e in body["evidence"]}
    assert by_key["code_reviewer"]["content_intact"] and by_key["test_engineer"]["content_intact"]
    if tamper != "swap_ref":
        assert by_key["security_reviewer"]["content_intact"] is False  # ... and which output is damaged

    response = resolve_via_api(client, auth_headers, approval)  # ... and approving is refused
    assert response.status_code == 409 and response.json()["error"]["code"] == "fingerprint_mismatch"
    after = snapshot(session_factory, run.id)
    assert after["approvals"][0].status == ApprovalStatus.PENDING and after["approvals"][0].resolved_by is None
    assert after["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert after["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert totals(after) == totals(before) and drain(worker) == 0  # nothing advanced

    if tamper == "delete_row":
        return  # cannot be restored
    if tamper in ("edit_file", "delete_file"):
        path.write_bytes(original_bytes)
    elif tamper == "swap_ref":
        db.execute(
            text("UPDATE workflow_node_runs SET output_snapshot_ref = :ref WHERE id = :id"),
            {"ref": artifact.id, "id": victim.id},
        )
        db.commit()
    else:
        db.execute(text("UPDATE artifacts SET content_hash = :h WHERE id = :id"), {"h": original_hash, "id": artifact.id})
        db.commit()
    assert get_evidence(client, auth_headers, approval.id).json()["fingerprint_matches"] is True
    assert resolve_via_api(client, auth_headers, approval).status_code == 200  # the SAME approval now decides normally
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED


def test_decisions_are_idempotent_and_opposites_conflict(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    _graph, run, approval = waiting_gate(db, session_factory, bootstrap.project, worker, "b4")
    assert resolve_via_api(client, auth_headers, approval).status_code == 200
    before = snapshot(session_factory, run.id)
    assert resolve_via_api(client, auth_headers, approval).status_code == 200  # replay
    assert resolve_via_api(client, auth_headers, approval, approve=False).status_code == 409  # opposite
    after = snapshot(session_factory, run.id)
    assert after["run"] == WorkflowRunStatus.COMPLETED and totals(after) == totals(before)
    assert after["approvals"][0].resolved_at == before["approvals"][0].resolved_at  # provenance kept


def _foreign_project(db, bootstrap, name, role=None):
    project = Project(org_id=bootstrap.organization.id, name=name)
    db.add(project)
    db.flush()
    if role is not None:
        db.add(ProjectMembership(project_id=project.id, user_id=bootstrap.user.id, role=role))
    db.commit()
    return project


def test_evidence_endpoint_authentication_and_authorization(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    # a gate in a project the caller has NO membership in
    foreign = _foreign_project(db, bootstrap, "b5-foreign")
    fgraph = stub_gate_graph(db, foreign, "b5f")
    frun = start(db, fgraph.built)
    drain(worker)
    fapproval = snapshot(session_factory, frun.id)["approvals"][0]
    fstate = snapshot(session_factory, frun.id)

    assert client.get(f"/approvals/{fapproval.id}/evidence").status_code == 401
    assert (
        client.get(f"/approvals/{fapproval.id}/evidence", headers={"Authorization": "Bearer nope"}).status_code == 401
    )
    assert get_evidence(client, auth_headers, "does-not-exist").status_code == 404
    denied = get_evidence(client, auth_headers, fapproval.id)  # cross-project isolation
    assert denied.status_code == 403
    for leaked in [fapproval.action_fingerprint] + [fstate["node_runs"][k].output_snapshot_ref for k in REVIEWERS]:
        assert leaked not in denied.text

    # READ is enough to see the evidence; deciding still needs MODIFY
    viewer_project = _foreign_project(db, bootstrap, "b5-viewer", ProjectRole.VIEWER)
    vgraph = stub_gate_graph(db, viewer_project, "b5v")
    vrun = start(db, vgraph.built)
    drain(worker)
    vapproval = snapshot(session_factory, vrun.id)["approvals"][0]
    ok = get_evidence(client, auth_headers, vapproval.id)
    assert ok.status_code == 200 and len(ok.json()["evidence"]) == 3
    assert resolve_via_api(client, auth_headers, vapproval).status_code == 403
    assert snapshot(session_factory, vrun.id)["approvals"][0].status == ApprovalStatus.PENDING


def test_the_evidence_endpoint_never_returns_artifact_content(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    _graph, _run, approval = waiting_gate(db, session_factory, bootstrap.project, worker, "b6")
    body = get_evidence(client, auth_headers, approval.id).text
    assert "OUTPUT-OF-" not in body  # the fake agents' artifact text is never inlined
    assert "storage" not in body and "content\"" not in body  # no paths, no content field


def test_openapi_declares_the_evidence_operation(client):
    assert "/approvals/{approval_id}/evidence" in client.get("/openapi.json").json()["paths"]


# =============================================================================
# C. Multi-parent gate mechanics
# =============================================================================


def drive_to_branches(db, session_factory, project, tmp_path, label, nodes=None, edges=None):
    graph = stub_gate_graph(db, project, label, nodes=nodes, edges=edges)
    run = start(db, graph.built)
    complete(db, session_factory, run.id, "planner", tmp_path=tmp_path)
    complete(db, session_factory, run.id, "engineer", tmp_path=tmp_path)
    return graph, run


def test_the_gate_appears_only_after_all_sources_and_creates_no_execution(
    db, session_factory, bootstrap, tmp_path
):
    _graph, run = drive_to_branches(db, session_factory, bootstrap.project, tmp_path, "c1")
    for key in ("code_reviewer", "security_reviewer"):
        complete(db, session_factory, run.id, key, tmp_path=tmp_path)
        mid = snapshot(session_factory, run.id)
        assert mid["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and mid["approvals"] == []
    before = snapshot(session_factory, run.id)

    complete(db, session_factory, run.id, "test_engineer", tmp_path=tmp_path)  # the LAST source

    after = snapshot(session_factory, run.id)
    assert after["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert len(after["approvals"]) == 1
    assert delta(before, after) == (0, 0, 0)  # zero TaskRuns, AgentRuns, jobs for the gate
    assert after["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert after["node_runs"]["gate"].agent_run_id is None


def test_simultaneous_completion_of_all_sources_creates_exactly_one_gate_and_approval(
    db, session_factory, bootstrap, tmp_path
):
    for round_number in range(8):
        graph, run = drive_to_branches(db, session_factory, bootstrap.project, tmp_path, f"c2-{round_number}")
        agents = {key: finish(db, session_factory, run.id, key, tmp_path=tmp_path) for key in REVIEWERS}
        before = snapshot(session_factory, run.id)

        def report(i, agents=agents):
            session = session_factory()
            try:
                WorkflowExecutionService(session).on_agent_run_complete(agents[REVIEWERS[i]].id)
            finally:
                session.close()

        in_threads(3, report)

        state = snapshot(session_factory, run.id)
        assert len(state["approvals"]) == 1, round_number
        assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
        assert delta(before, state) == (0, 0, 0)
        parent = graph.built.task_run.id
        assert count_events(session_factory, parent, "workflow.node.waiting_for_approval") == 1
        assert count_events(session_factory, parent, "approval.requested") == 1


def test_concurrent_dispatch_of_one_multi_parent_gate_creates_one_wait_and_one_approval(
    db, session_factory, bootstrap, tmp_path, monkeypatch
):
    graph, run = drive_to_branches(db, session_factory, bootstrap.project, tmp_path, "c3")
    real = WorkflowExecutionService._dispatch_human_approval
    # make the three completions leave the gate ready-but-undispatched (the process "dies" at that step)
    monkeypatch.setattr(WorkflowExecutionService, "_dispatch_human_approval", lambda self, *a, **k: None)
    for key in REVIEWERS:
        complete(db, session_factory, run.id, key, tmp_path=tmp_path)
    monkeypatch.setattr(WorkflowExecutionService, "_dispatch_human_approval", real)
    assert nodes_of(session_factory, run.id)["gate"] == WorkflowNodeRunStatus.PENDING
    gate_node_run_id = snapshot(session_factory, run.id)["node_runs"]["gate"].id

    def dispatch(_i):
        session = session_factory()
        try:
            WorkflowExecutionService(session)._dispatch_node_for_execution(session.get(WorkflowNodeRun, gate_node_run_id))
        finally:
            session.close()

    in_threads(5, dispatch)

    state = snapshot(session_factory, run.id)
    assert len(state["approvals"]) == 1 and state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert count_events(session_factory, graph.built.task_run.id, "approval.requested") == 1


POST_NODES = [
    ("planner", AGENT),
    ("engineer", AGENT),
    ("test_engineer", AGENT),
    ("security_reviewer", AGENT),
    ("code_reviewer", AGENT),
    ("gate", APPROVAL),
    ("post", AGENT),
    ("result", TERMINAL),
]
POST_EDGES = (
    [("planner", "engineer")]
    + [("engineer", r) for r in REVIEWERS]
    + [(r, "gate") for r in REVIEWERS]
    + [("gate", "post"), ("post", "result")]
)


def test_a_node_after_a_multi_parent_gate_receives_every_approved_output(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    graph = stub_gate_graph(db, bootstrap.project, "c4", nodes=POST_NODES, edges=POST_EDGES)
    run = start(db, graph.built)
    drain(worker)
    waiting = snapshot(session_factory, run.id)
    assert resolve_via_api(client, auth_headers, waiting["approvals"][0]).status_code == 200
    drain(worker)

    final = snapshot(session_factory, run.id)
    assert final["run"] == WorkflowRunStatus.COMPLETED and calls.count("post") == 1
    context = db.get(AgentRun, final["node_runs"]["post"].agent_run_id).input_context_json
    assert [u["node_key"] for u in context["upstream"]] == list(REVIEWERS)  # ALL three, by producing node
    assert context["upstream_artifact_ids"] == [waiting["node_runs"][k].output_snapshot_ref for k in REVIEWERS]
    assert final["node_runs"]["gate"].output_snapshot_ref is None  # an approval of a SET has no artifact of its own


def test_a_single_input_gate_after_a_multi_parent_gate_binds_the_same_three_outputs(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    nodes = [n for n in GATE_ACCEPTANCE_NODES if n[0] != "result"] + [("gate2", APPROVAL), ("result", TERMINAL)]
    edges = [e for e in GATE_ACCEPTANCE_EDGES if e[0] != "gate"] + [("gate", "gate2"), ("gate2", "result")]
    graph = stub_gate_graph(db, bootstrap.project, "c5", nodes=nodes, edges=edges)
    run = start(db, graph.built)
    drain(worker)
    first = snapshot(session_factory, run.id)["approvals"][0]
    assert resolve_via_api(client, auth_headers, first).status_code == 200

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["gate2"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL and len(state["approvals"]) == 2
    second = next(a for a in state["approvals"] if a.id != first.id)
    body = get_evidence(client, auth_headers, second.id).json()
    assert [e["node_key"] for e in body["evidence"]] == list(REVIEWERS)  # resolved through the first gate
    assert body["fingerprint_matches"] is True
    assert resolve_via_api(client, auth_headers, second).status_code == 200
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED


def test_gate_configuration_is_still_validated_for_a_multi_parent_gate(db, bootstrap):
    definitions = WorkflowDefinitionService(db)
    workflow = definitions.create_workflow(bootstrap.project.id, "c6")
    version = definitions.get_latest_version(workflow.id)
    parents = [
        definitions.add_node(
            workflow.id,
            version.version,
            key,
            WorkflowNodeType.AGENT,
            config={"agent_version_id": make_runnable_agent_version(db, make_agent(db, bootstrap.project, key)).id},
        )
        for key in ("a", "b")
    ]
    gate = definitions.add_node(
        workflow.id,
        version.version,
        "gate",
        WorkflowNodeType.HUMAN_APPROVAL,
        config={"approval_group": "g", "auto_approve": True},  # still forbidden, however many parents
    )
    end = definitions.add_node(workflow.id, version.version, "end", WorkflowNodeType.TERMINAL)
    for parent in parents:
        definitions.add_edge(workflow.id, version.version, parent.id, gate.id)
    definitions.add_edge(workflow.id, version.version, gate.id, end.id)
    with pytest.raises(DAGValidationError) as exc:
        definitions.publish_version(workflow.id, version.version)
    assert any("automatic or timed approval" in issue for issue in exc.value.issues)


# =============================================================================
# D. Recovery / reconciliation / concurrency around the gate
# =============================================================================


def sweep(session_factory):
    return new_worker(session_factory).reconcile_workflows()


def test_all_branches_complete_but_the_approval_was_never_created(db, session_factory, bootstrap, tmp_path):
    graph, run = drive_to_branches(db, session_factory, bootstrap.project, tmp_path, "d1")
    for key in REVIEWERS:
        finish(db, session_factory, run.id, key, tmp_path=tmp_path)  # agents done; no callback ever ran
    before = snapshot(session_factory, run.id)
    assert before["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and before["approvals"] == []

    for _ in range(3):
        sweep(session_factory)
    in_threads(4, lambda _i: sweep(session_factory))

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL and len(state["approvals"]) == 1
    assert delta(before, state) == (0, 0, 0)
    assert count_events(session_factory, graph.built.task_run.id, "approval.requested") == 1


@pytest.mark.parametrize("approve", [True, False])
def test_approval_resolved_but_the_gate_was_never_advanced(db, session_factory, bootstrap, tmp_path, approve):
    graph, run = drive_to_branches(db, session_factory, bootstrap.project, tmp_path, f"d2-{approve}")
    for key in REVIEWERS:
        complete(db, session_factory, run.id, key, tmp_path=tmp_path)
    approval = snapshot(session_factory, run.id)["approvals"][0]
    db.execute(
        text("UPDATE approvals SET status=:s, resolved_by=:u, resolved_at=CURRENT_TIMESTAMP WHERE id=:id"),
        {"s": "approved" if approve else "rejected", "u": bootstrap.user.id, "id": approval.id},
    )
    db.commit()
    before = snapshot(session_factory, run.id)
    assert before["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL

    for _ in range(2):
        sweep(session_factory)

    state = snapshot(session_factory, run.id)
    if approve:
        assert state["nodes"]["gate"] == state["nodes"]["result"] == WorkflowNodeRunStatus.COMPLETED
        assert state["run"] == WorkflowRunStatus.COMPLETED
    else:
        assert state["nodes"]["gate"] == WorkflowNodeRunStatus.FAILED
        assert state["run"] == WorkflowRunStatus.FAILED and state["nodes"]["result"] == WorkflowNodeRunStatus.PENDING
        assert count_events(session_factory, graph.built.task_run.id, "workflow.failed") == 1
    assert delta(before, state) == (0, 0, 0)


def test_a_waiting_gate_survives_a_restart_untouched_and_can_then_be_decided(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    graph, run, approval = waiting_gate(db, session_factory, bootstrap.project, worker, "d3")
    before = snapshot(session_factory, run.id)
    types_before = event_types(session_factory, graph.built.task_run.id)
    for _ in range(4):  # "restart": brand-new workers sweep
        assert sweep(session_factory) == 1
    after = snapshot(session_factory, run.id)
    assert after["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert [a.id for a in after["approvals"]] == [approval.id] and after["nodes"] == before["nodes"]
    assert totals(after) == totals(before)
    assert event_types(session_factory, graph.built.task_run.id) == types_before  # no duplicate evidence

    assert resolve_via_api(client, auth_headers, approval).status_code == 200
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED


def test_approved_but_the_downstream_node_was_never_dispatched(
    db, session_factory, bootstrap, worker, calls, monkeypatch
):
    graph = stub_gate_graph(db, bootstrap.project, "d4", nodes=POST_NODES, edges=POST_EDGES)
    run = start(db, graph.built)
    drain(worker)
    approval = snapshot(session_factory, run.id)["approvals"][0]
    real = WorkflowExecutionService.after_approval_resolved
    monkeypatch.setattr(WorkflowExecutionService, "after_approval_resolved", lambda self, *a, **k: None)
    session = session_factory()
    try:  # the decision commits atomically with the gate; the process then dies before resuming
        ApprovalService(session).resolve(
            approval.id, user=bootstrap.user, approve=True, action_fingerprint=approval.action_fingerprint
        )
    finally:
        session.close()
    monkeypatch.setattr(WorkflowExecutionService, "after_approval_resolved", real)
    stopped = snapshot(session_factory, run.id)
    assert stopped["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED and stopped["nodes"]["post"] == WorkflowNodeRunStatus.PENDING

    for _ in range(3):
        sweep(session_factory)
    in_threads(4, lambda _i: sweep(session_factory))

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["post"] == WorkflowNodeRunStatus.RUNNING
    assert delta(stopped, state) == (1, 1, 1)  # dispatched ONCE
    drain(worker)
    assert calls.count("post") == 1 and snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED


INDEPENDENT_NODES = [
    ("entry", AGENT),
    ("a", AGENT),
    ("b", AGENT),
    ("c", AGENT),
    ("gate", APPROVAL),
    ("end", TERMINAL),
]
INDEPENDENT_EDGES = [
    ("entry", "a"),
    ("entry", "b"),
    ("entry", "c"),
    ("a", "gate"),
    ("b", "gate"),
    ("gate", "end"),
    ("c", "end"),
]


def independent_run(db, session_factory, project, tmp_path, label):
    """a, b -> gate (multi-parent) while an independent branch c is still running."""
    graph = build_graph(db, project, INDEPENDENT_NODES, INDEPENDENT_EDGES, label=label, real=False)
    run = start(db, graph.built)
    complete(db, session_factory, run.id, "entry", tmp_path=tmp_path)
    complete(db, session_factory, run.id, "a", tmp_path=tmp_path)
    complete(db, session_factory, run.id, "b", tmp_path=tmp_path)
    state = snapshot(session_factory, run.id)
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert state["run"] == WorkflowRunStatus.RUNNING  # c can still progress: the summary is NOT "waiting"
    return graph, run, state["approvals"][0]


def test_rejected_but_failure_propagation_was_interrupted(
    db, session_factory, bootstrap, tmp_path, monkeypatch
):
    graph, run, approval = independent_run(db, session_factory, bootstrap.project, tmp_path, "d5")
    real = WorkflowExecutionService.after_approval_resolved
    monkeypatch.setattr(WorkflowExecutionService, "after_approval_resolved", lambda self, *a, **k: None)
    session = session_factory()
    try:
        ApprovalService(session).resolve(
            approval.id, user=bootstrap.user, approve=False, action_fingerprint=approval.action_fingerprint
        )
    finally:
        session.close()
    monkeypatch.setattr(WorkflowExecutionService, "after_approval_resolved", real)

    crashed = snapshot(session_factory, run.id)
    assert crashed["nodes"]["gate"] == WorkflowNodeRunStatus.FAILED
    assert crashed["run"] == WorkflowRunStatus.RUNNING and agent_of(db, session_factory, run.id, "c").cancellation_requested_at is None

    for _ in range(3):
        sweep(session_factory)
    state = snapshot(session_factory, run.id)
    assert agent_of(db, session_factory, run.id, "c").cancellation_requested_at is not None  # c told to stop
    assert state["run"] == WorkflowRunStatus.RUNNING  # ... and still draining
    assert state["nodes"]["a"] == state["nodes"]["b"] == WorkflowNodeRunStatus.COMPLETED  # outputs kept

    complete(db, session_factory, run.id, "c", AgentRunStatus.STOPPED)
    final = snapshot(session_factory, run.id)
    assert final["run"] == WorkflowRunStatus.FAILED and final["nodes"]["end"] == WorkflowNodeRunStatus.PENDING
    assert count_events(session_factory, graph.built.task_run.id, "workflow.failed") == 1


def test_cancellation_while_the_multi_parent_gate_is_waiting(
    client, db, session_factory, auth_headers, bootstrap, worker, calls
):
    _graph, run, approval = waiting_gate(db, session_factory, bootstrap.project, worker, "d6")
    outputs = {k: snapshot(session_factory, run.id)["node_runs"][k].output_snapshot_ref for k in REVIEWERS}

    WorkflowExecutionService(db).cancel_workflow_run(run.id, cancelled_by_user_id=bootstrap.user.id)

    state = snapshot(session_factory, run.id)
    assert state["approvals"][0].status == ApprovalStatus.EXPIRED
    assert state["approvals"][0].resolution_note == "workflow_cancelled"
    assert state["nodes"]["gate"] == state["nodes"]["result"] == WorkflowNodeRunStatus.CANCELLED
    assert state["run"] == WorkflowRunStatus.CANCELLED and state["run_ended_at"] is not None
    assert {k: state["node_runs"][k].output_snapshot_ref for k in REVIEWERS} == outputs  # preserved
    assert resolve_via_api(client, auth_headers, approval).status_code == 409  # cannot approve a cancelled run


def test_concurrent_approval_and_reconciliation_resume_exactly_once(
    db, session_factory, bootstrap, tmp_path
):
    for round_number in range(5):
        graph = stub_gate_graph(db, bootstrap.project, f"d7-{round_number}", nodes=POST_NODES, edges=POST_EDGES)
        run = start(db, graph.built)
        for key in ("planner", "engineer", *REVIEWERS):
            complete(db, session_factory, run.id, key, tmp_path=tmp_path)
        approval = snapshot(session_factory, run.id)["approvals"][0]
        before = snapshot(session_factory, run.id)

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

        state = snapshot(session_factory, run.id)
        assert state["approvals"][0].status == ApprovalStatus.APPROVED, round_number
        assert state["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED and state["nodes"]["post"] == WorkflowNodeRunStatus.RUNNING
        assert delta(before, state) == (1, 1, 1), round_number  # downstream dispatched once
        parent = graph.built.task_run.id
        assert count_events(session_factory, parent, "approval.approved") == 1
        assert count_events(session_factory, parent, "workflow.node.completed", state["node_runs"]["gate"].id) == 1


def test_concurrent_approval_and_cancellation_never_leave_a_contradictory_state(
    db, session_factory, bootstrap, tmp_path
):
    for round_number in range(6):
        graph = stub_gate_graph(db, bootstrap.project, f"d8-{round_number}", nodes=POST_NODES, edges=POST_EDGES)
        run = start(db, graph.built)
        for key in ("planner", "engineer", *REVIEWERS):
            complete(db, session_factory, run.id, key, tmp_path=tmp_path)
        approval = snapshot(session_factory, run.id)["approvals"][0]

        def act(i, approval=approval, run_id=run.id):
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

        state = snapshot(session_factory, run.id)
        status, gate = state["approvals"][0].status, state["nodes"]["gate"]
        assert (status, gate) in {
            (ApprovalStatus.APPROVED, WorkflowNodeRunStatus.COMPLETED),
            (ApprovalStatus.EXPIRED, WorkflowNodeRunStatus.CANCELLED),
        }, (round_number, status, gate)
        assert state["run"] in (WorkflowRunStatus.CANCELLING, WorkflowRunStatus.CANCELLED)
        # 'post' is never left dispatched into a cancelled run without being cancelled/signalled
        if state["nodes"]["post"] == WorkflowNodeRunStatus.RUNNING:
            assert agent_of(db, session_factory, run.id, "post").cancellation_requested_at is not None


def test_repeated_sweeps_of_a_settled_gate_run_are_a_no_op(client, db, session_factory, auth_headers, bootstrap, worker, calls):
    graph, run, approval = waiting_gate(db, session_factory, bootstrap.project, worker, "d9")
    assert resolve_via_api(client, auth_headers, approval).status_code == 200
    settled = snapshot(session_factory, run.id)
    types_before = event_types(session_factory, graph.built.task_run.id)
    for _ in range(5):
        sweep(session_factory)
    assert snapshot(session_factory, run.id)["nodes"] == settled["nodes"]
    assert event_types(session_factory, graph.built.task_run.id) == types_before


# =============================================================================
# E. Worker concurrency
# =============================================================================


def test_concurrency_defaults_to_one_and_is_bounded():
    assert Settings().worker_concurrency == 1 and Worker().concurrency == 1
    assert Settings(worker_concurrency=4).worker_concurrency == 4
    for bad in (0, -1, 5):
        with pytest.raises(ValueError):
            Settings(worker_concurrency=bad)
        with pytest.raises(ValueError):
            Worker(concurrency=bad)
    assert Worker(concurrency=3).concurrency == 3


def test_concurrency_is_configurable_through_the_environment(monkeypatch):
    monkeypatch.setenv("MAP_WORKER_CONCURRENCY", "2")
    assert Settings().worker_concurrency == 2
    monkeypatch.setenv("MAP_WORKER_CONCURRENCY", "9")
    with pytest.raises(ValueError):
        Settings()


def test_cli_flag_parsing_and_precedence(monkeypatch):
    assert parse_args([]).concurrency is None  # -> falls back to the setting
    assert parse_args(["--concurrency", "3"]).concurrency == 3
    for bad in ("0", "5", "x", "-2"):
        with pytest.raises(SystemExit):
            parse_args(["--concurrency", bad])

    seen = []
    monkeypatch.setattr(logging_config_module, "configure_logging", lambda: None)  # never touch data/logs
    monkeypatch.setattr(Worker, "run_forever", lambda self: seen.append(self.concurrency))
    worker_module.main(["--concurrency", "3"])
    monkeypatch.setattr(settings, "worker_concurrency", 2)
    worker_module.main([])
    assert seen == [3, 2]  # flag wins; otherwise the configured value


def test_concurrency_one_is_the_unchanged_single_loop_no_lane_threads(db, session_factory):
    with running_worker(session_factory, 1):
        names = [t.name for t in threading.enumerate()]
        assert not any(name.startswith("worker-lane") for name in names)
        job = JobQueueRepository().enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref="one")
        assert wait_until(lambda: rows(session_factory, JobQueue, JobQueue.id == job.id)[0].status == JobQueueStatus.DONE)


def test_concurrency_three_runs_three_jobs_at_once_on_independent_lanes(db, session_factory, monkeypatch):
    meeting = threading.Barrier(3, timeout=20)  # opens only if 3 jobs are inside AT THE SAME TIME
    in_flight, peak, lock, owners, thread_ids = [0], [0], threading.Lock(), [], set()
    real = Worker._run_internal_test_job
    first_three = []

    def instrumented(self, job):
        with lock:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
            owners.append(self.worker_id)
            thread_ids.add(threading.get_ident())
            proves = len(first_three) < 3
            if proves:
                first_three.append(job.id)
        try:
            if proves:
                meeting.wait()
            return real(self, job)
        finally:
            with lock:
                in_flight[0] -= 1

    monkeypatch.setattr(Worker, "_run_internal_test_job", instrumented)
    repo = JobQueueRepository()
    jobs = [repo.enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref=f"j{i}") for i in range(7)]

    with running_worker(session_factory, 3) as the_worker:
        assert wait_until(
            lambda: {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE}
        )
        assert [t.name for t in threading.enumerate() if t.name.startswith("worker-lane")]  # lanes exist

    assert peak[0] == 3  # never more lanes than configured; all three really overlapped
    assert len(owners) == 7 and len(set(owners)) == 3  # every job ran exactly once, on one of 3 lane ids
    assert all(owner.startswith(the_worker.worker_id + ":lane") for owner in owners)
    assert len(thread_ids) == 3
    done = rows(session_factory, JobQueue)
    assert sorted(j.id for j in done) == sorted(j.id for j in jobs)
    assert all(j.attempt_count == 1 for j in done)  # no job was claimed twice


def test_shutdown_stops_every_lane_and_leaves_no_job_leased(db, session_factory):
    worker = Worker(session_factory=session_factory, poll_interval_seconds=0.01, concurrency=3, reconcile_interval_seconds=0)
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    assert wait_until(lambda: len([t for t in threading.enumerate() if t.name.startswith("worker-lane")]) >= 3)
    worker.request_shutdown()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert not [t for t in threading.enumerate() if t.name.startswith("worker-lane")]
    assert rows(session_factory, JobQueue, JobQueue.status == JobQueueStatus.LEASED) == []


def test_a_failing_lane_stops_the_worker_loudly_instead_of_degrading_silently(session_factory, monkeypatch):
    def boom(self):
        raise RuntimeError("lane exploded")

    monkeypatch.setattr(Worker, "run_once", boom)
    worker = Worker(session_factory=session_factory, poll_interval_seconds=0.01, concurrency=3, reconcile_interval_seconds=0)
    outcome = []

    def run():
        try:
            worker.run_forever()
        except RuntimeError as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], RuntimeError)
    assert not [t for t in threading.enumerate() if t.name.startswith("worker-lane")]


def test_lanes_create_their_own_sessions_inside_their_own_threads(db, session_factory):
    made = []  # holds the sessions themselves, so object identity stays meaningful (ids get reused after GC)

    def recording_factory():
        session = session_factory()
        made.append((threading.get_ident(), session))
        return session

    repo = JobQueueRepository()
    for i in range(6):
        repo.enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref=f"s{i}")
    worker = Worker(
        session_factory=recording_factory,
        poll_interval_seconds=0.01,
        concurrency=3,
        reconcile_interval_seconds=0,
        internal_test_work_seconds=0.02,
        internal_test_iterations=1,
    )
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    assert wait_until(lambda: {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE})
    worker.request_shutdown()
    thread.join(timeout=10)
    assert len({id(session) for _thread, session in made}) == len(made)  # a NEW session per use, never shared
    assert len({thread_id for thread_id, _session in made}) >= 3  # created on several lane threads


# =============================================================================
# F. Upstream-context cap
# =============================================================================


def run_aggregator_workflow(db, session_factory, bootstrap, monkeypatch, artifacts_dir, label, *, text, window=None):
    graph = build_acceptance(db, bootstrap.project, label=label)
    if window is not None:
        model = db.query(Model).filter(Model.canonical_model_id == f"fake/{label}-aggregator").one()
        model.context_window = window
        db.commit()
    provider = install_provider(monkeypatch, RoleProvider(graph.role_of, text=text))
    run = start(db, graph.built)
    solo = new_worker(session_factory)
    while solo.run_once():
        pass
    return graph, provider, run


def big(role):
    return ("X" * 800) if role in REVIEWERS else f"OUTPUT-OF-{role}"


def failed_attempt_category(session_factory, run_id):
    node_run = snapshot(session_factory, run_id)["node_runs"]["aggregator"]
    attempt = rows(session_factory, AgentRunAttempt, AgentRunAttempt.agent_run_id == node_run.agent_run_id)[0]
    return attempt.error["category"], attempt.error["message"]


def test_evidence_over_the_platform_cap_fails_closed_before_inference(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    monkeypatch.setattr(settings, "workflow_upstream_context_max_chars", 2000)  # 3 x 800 + labels > 2000
    _graph, provider, run = run_aggregator_workflow(
        db, session_factory, bootstrap, monkeypatch, artifacts_dir, "f1", text=big
    )
    state = snapshot(session_factory, run.id)
    assert state["nodes"]["aggregator"] == WorkflowNodeRunStatus.FAILED
    assert state["run"] == WorkflowRunStatus.FAILED  # fail fast like any node failure
    assert provider.count("aggregator") == 0  # NO provider call
    category, message = failed_attempt_category(session_factory, run.id)
    assert category == "workflow_context_too_large"
    assert "3 artifact(s)" in message and "limit of 2000" in message and "platform cap" in message
    assert "never truncated" in message and "XXXX" not in message  # actionable, and no evidence content leaked
    assert db.get(AgentRun, state["node_runs"]["aggregator"].agent_run_id).status == AgentRunStatus.FAILED


def test_evidence_within_the_cap_is_delivered_complete_and_untruncated(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    monkeypatch.setattr(settings, "workflow_upstream_context_max_chars", 100_000)
    _graph, provider, run = run_aggregator_workflow(
        db, session_factory, bootstrap, monkeypatch, artifacts_dir, "f2", text=big
    )
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.COMPLETED
    assert provider.prompt_of("aggregator").count("X") == 3 * 800  # every character of all three outputs


def test_a_known_model_context_window_can_only_lower_the_limit(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    monkeypatch.setattr(settings, "workflow_upstream_context_max_chars", 100_000)
    _graph, provider, run = run_aggregator_workflow(
        db, session_factory, bootstrap, monkeypatch, artifacts_dir, "f3", text=big, window=500  # 500 tokens
    )
    assert snapshot(session_factory, run.id)["nodes"]["aggregator"] == WorkflowNodeRunStatus.FAILED
    assert provider.count("aggregator") == 0
    category, message = failed_attempt_category(session_factory, run.id)
    assert category == "workflow_context_too_large" and "model context window (500 tokens)" in message


def test_a_huge_model_window_never_raises_the_platform_cap(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    monkeypatch.setattr(settings, "workflow_upstream_context_max_chars", 2000)
    _graph, provider, run = run_aggregator_workflow(
        db, session_factory, bootstrap, monkeypatch, artifacts_dir, "f4", text=big, window=10_000_000
    )
    assert snapshot(session_factory, run.id)["nodes"]["aggregator"] == WorkflowNodeRunStatus.FAILED
    assert provider.count("aggregator") == 0
    assert "platform cap" in failed_attempt_category(session_factory, run.id)[1]


def test_an_unknown_model_window_is_held_to_the_platform_cap_only(db, monkeypatch):
    """No limit is invented for a model without recorded metadata."""
    service = AgentExecutionService(db)
    monkeypatch.setattr(settings, "workflow_upstream_context_max_chars", 10_000)

    class Resolved:  # only the attributes the check reads
        model = type("M", (), {"context_window": None})()

    service._enforce_upstream_context_limit(9_999, 3, Resolved())  # under the cap: fine
    service._enforce_upstream_context_limit(10_000, 3, None)  # exactly at the cap: fine
    with pytest.raises(_WorkflowContextTooLarge):
        service._enforce_upstream_context_limit(10_001, 3, Resolved())


def test_the_cap_setting_is_bounded():
    assert Settings().workflow_upstream_context_max_chars == 200_000
    for bad in (0, 999, 10_000_001):
        with pytest.raises(ValueError):
            Settings(workflow_upstream_context_max_chars=bad)


def test_an_over_cap_failure_is_final_one_attempt_no_retry(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    """A non-retryable category: the node is failed once (one attempt, one
    AgentRun) -- the evidence will not shrink by retrying."""
    monkeypatch.setattr(settings, "workflow_upstream_context_max_chars", 2000)
    _graph, provider, run = run_aggregator_workflow(
        db, session_factory, bootstrap, monkeypatch, artifacts_dir, "f5", text=big
    )
    node_run = snapshot(session_factory, run.id)["node_runs"]["aggregator"]
    attempts = rows(session_factory, AgentRunAttempt, AgentRunAttempt.agent_run_id == node_run.agent_run_id)
    assert len(attempts) == 1  # a non-retryable category: no second attempt
    assert provider.count("aggregator") == 0
    assert snapshot(session_factory, run.id)["run"] == WorkflowRunStatus.FAILED

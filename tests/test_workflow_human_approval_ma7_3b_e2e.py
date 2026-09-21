"""MA7.3b acceptance: Planner -> HUMAN_APPROVAL -> Engineer -> TERMINAL through
the REAL production path.

Real: WorkflowExecutionService, the Worker (claim/lease/fencing/completion and
its workflow reconciliation), AgentExecutionService (model resolution, budget,
prompt assembly, artifact files + sha256), ApprovalService, the HTTP API, the
database. Stubbed: ONLY the external model provider (a FakeAdapter behind the
execution service's ``adapter_factory`` seam) -- no network, no real model.
Every database is a disposable temp file.
"""

from decimal import Decimal

import pytest

import app.services.execution_service as execution_service_module
from app.config import settings
from app.db.enums import (
    AgentRunStatus,
    ApprovalStatus,
    TaskRunStatus,
    VersionStatus,
    WorkflowNodeRunStatus,
    WorkflowRunStatus,
)
from app.models.artifacts_eval import Artifact
from app.models.tasks import AgentRun, TaskRun
from app.providers.base import InvokeResponse
from app.services.workflow_execution_service import WorkflowExecutionService
from app.worker import Worker
from tests.conftest import make_model, make_provider, make_provider_model
from tests.ma7_3b_support import (
    AGENT,
    APPROVAL,
    TERMINAL,
    audit_types,
    build_chain,
    drain,
    event_types,
    resolve_via_api,
    snapshot,
    start,
)

PLANNER_OUTPUT = "PLAN: 1) add the endpoint 2) write tests -- APPROVED-CONTENT-MARKER"
ENGINEER_OUTPUT = "ENGINEER: implemented the plan"


class FakeAdapter:
    """The provider boundary. Records every request the real execution
    service builds; answers from a per-call script."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return InvokeResponse(text=self.texts.pop(0), tokens_in=11, tokens_out=7, latency_ms=3)


@pytest.fixture()
def provider_adapter(monkeypatch, tmp_path):
    """Real AgentExecutionService everywhere (it is what the Worker builds),
    with its provider adapter factory pointed at a FakeAdapter and artifact
    files written under tmp_path, never data/artifacts."""
    monkeypatch.setattr(settings, "artifacts_dir", tmp_path / "artifacts")
    adapter = FakeAdapter([PLANNER_OUTPUT, ENGINEER_OUTPUT])
    original_init = execution_service_module.AgentExecutionService.__init__

    def init_with_fake_provider(self, db, adapter_factory=None):
        original_init(self, db, adapter_factory=lambda _db, _provider: adapter)

    monkeypatch.setattr(execution_service_module.AgentExecutionService, "__init__", init_with_fake_provider)
    return adapter


def _activate_agents(db, built):
    """Point each agent version at a free provider model (manual selection)."""
    model = make_model(db, canonical_model_id="free/model")
    provider = make_provider(db)
    provider_model = make_provider_model(
        db, model=model, provider=provider, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0)
    )
    for agent_version in built.agent_versions.values():
        agent_version.status = VersionStatus.ACTIVE
        agent_version.model_policy = {"mode": "manual", "manual_provider_model_id": provider_model.id}
    db.commit()


def _worker(session_factory):
    return Worker(
        session_factory=session_factory,
        poll_interval_seconds=0.01,
        lease_seconds=5,
        heartbeat_interval_seconds=0.05,
    )


def test_planner_approval_engineer_terminal_end_to_end(
    db, session_factory, client, auth_headers, bootstrap, provider_adapter
):
    built = build_chain(
        db,
        bootstrap.project,
        [("planner", AGENT), ("gate", APPROVAL), ("engineer", AGENT), ("result", TERMINAL)],
        label="e2e",
        activate_agents=True,
    )
    _activate_agents(db, built)
    run = start(db, built)
    task_run_id = built.task_run.id

    # -- 1. Planner executes through the real Worker ---------------------------
    worker = _worker(session_factory)
    assert drain(worker) == 1  # exactly one job: the Planner. The gate creates none.
    assert len(provider_adapter.requests) == 1
    assert "APPROVED-CONTENT-MARKER" not in provider_adapter.requests[0].user_prompt

    # -- 2. the workflow is durably waiting on exactly one PENDING approval ----
    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert state["nodes"] == {
        "planner": WorkflowNodeRunStatus.COMPLETED,
        "gate": WorkflowNodeRunStatus.WAITING_FOR_APPROVAL,
        "engineer": WorkflowNodeRunStatus.PENDING,
        "result": WorkflowNodeRunStatus.PENDING,
    }
    assert len(state["approvals"]) == 1
    approval = state["approvals"][0]
    assert approval.status == ApprovalStatus.PENDING
    assert approval.scope_ref_id == state["node_runs"]["gate"].id
    planner_artifact_id = state["node_runs"]["planner"].output_snapshot_ref
    assert approval.bound_artifact_id == planner_artifact_id

    # the approval node performs no execution: no AgentRun / TaskRun / job for it
    assert state["node_runs"]["gate"].agent_run_id is None
    assert state["agent_runs_total"] == 1  # only the Planner's
    assert state["task_runs_total"] == 2  # the workflow's own + the Planner node's
    assert state["jobs_total"] == 1

    # the Engineer has NOT executed, and the worker has nothing to do
    assert worker.run_once() is False
    assert len(provider_adapter.requests) == 1
    assert state["node_runs"]["engineer"].agent_run_id is None

    # -- 3. the wait survives "restart": fresh sessions, a brand-new Worker, ---
    #       and repeated reconciliation sweeps create nothing.
    for _ in range(3):
        fresh = session_factory()
        try:
            assert WorkflowExecutionService(fresh).reconcile_active_runs() == 1
        finally:
            fresh.close()
    restarted_worker = _worker(session_factory)
    assert restarted_worker.reconcile_workflows() == 1
    assert restarted_worker.run_once() is False

    after = snapshot(session_factory, run.id)
    assert after["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    assert [a.id for a in after["approvals"]] == [approval.id]
    assert after["approvals"][0].status == ApprovalStatus.PENDING
    assert (after["agent_runs_total"], after["jobs_total"]) == (1, 1)
    assert len(provider_adapter.requests) == 1

    # the pending approval is discoverable through the API a browser would use
    listed = client.get(f"/approvals?project_id={bootstrap.project.id}", headers=auth_headers).json()
    assert [a["id"] for a in listed] == [approval.id]
    nodes_api = client.get(f"/workflow-runs/{run.id}/nodes", headers=auth_headers).json()
    assert {n["approval_id"] for n in nodes_api if n["approval_id"]} == {approval.id}

    # -- 4. an authorized human approves; the existing engine resumes ----------
    response = resolve_via_api(client, auth_headers, approval, approve=True, notes="plan looks right")
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["resolved_by"], body["resolution_note"]) == (
        "approved",
        bootstrap.user.id,
        "plan looks right",
    )

    resumed = snapshot(session_factory, run.id)
    assert resumed["run"] == WorkflowRunStatus.RUNNING
    assert resumed["nodes"]["gate"] == WorkflowNodeRunStatus.COMPLETED
    assert resumed["nodes"]["engineer"] == WorkflowNodeRunStatus.RUNNING  # dispatched by the approval
    assert resumed["agent_runs_total"] == 2 and resumed["jobs_total"] == 2
    # pass-through: the gate's output IS the approved Planner artifact (no new artifact)
    assert resumed["node_runs"]["gate"].output_snapshot_ref == planner_artifact_id

    # -- 5. the Engineer executes exactly once, through the real Worker --------
    assert drain(worker) == 1
    assert len(provider_adapter.requests) == 2
    engineer_prompt = provider_adapter.requests[1].user_prompt
    # ... and its ACTUAL prompt carries the approved Planner output (content, not just an id)
    assert PLANNER_OUTPUT in engineer_prompt
    assert planner_artifact_id in engineer_prompt

    # -- 6. TERMINAL completes; the WorkflowRun completes ---------------------
    final = snapshot(session_factory, run.id)
    assert final["run"] == WorkflowRunStatus.COMPLETED
    assert final["run_ended_at"] is not None
    assert final["nodes"] == {
        "planner": WorkflowNodeRunStatus.COMPLETED,
        "gate": WorkflowNodeRunStatus.COMPLETED,
        "engineer": WorkflowNodeRunStatus.COMPLETED,
        "result": WorkflowNodeRunStatus.COMPLETED,
    }
    assert final["node_runs"]["result"].agent_run_id is None
    assert (final["agent_runs_total"], final["jobs_total"]) == (2, 2)
    assert worker.run_once() is False

    # lineage: Engineer's run recorded the approved artifact + the gate node run
    verify = session_factory()
    try:
        engineer_run = verify.get(AgentRun, final["node_runs"]["engineer"].agent_run_id)
        assert engineer_run.status == AgentRunStatus.COMPLETED
        assert engineer_run.input_context_json["upstream_artifact_ids"] == [planner_artifact_id]
        assert engineer_run.input_context_json["upstream_node_run_ids"] == [final["node_runs"]["gate"].id]
        # exactly the two artifacts the two agents produced -- the gate fabricated none
        assert verify.query(Artifact).count() == 2
        assert (
            verify.get(TaskRun, built.task_run.id).status == TaskRunStatus.CREATED
        )  # parent untouched (MA7.2)
    finally:
        verify.close()

    # -- 7. evidence: Flight Recorder + audit ---------------------------------
    types = event_types(session_factory, task_run_id)
    for expected in (
        "workflow.started",
        "workflow.node.waiting_for_approval",
        "approval.requested",
        "approval.approved",
        "workflow.node.completed",
        "workflow.completed",
    ):
        assert expected in types, (expected, types)
    assert (
        types.index("approval.requested")
        < types.index("approval.approved")
        < types.index("workflow.completed")
    )
    assert audit_types(session_factory) == ["approval.approved"]

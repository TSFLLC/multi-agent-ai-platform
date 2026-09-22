"""Shared builders for the MA7.3b (Human Approval workflow integration) tests.

Not a test module (no ``test_`` prefix): imported by
``test_workflow_human_approval_ma7_3b*.py``. Everything here builds rows through
the real definition/execution services -- never raw workflow state -- except
where a test deliberately simulates a crash by editing state directly.
"""

import hashlib
from collections import namedtuple
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db.enums import (
    AgentRunStatus,
    ArtifactType,
    JobType,
    TaskRunStatus,
    WorkflowNodeType,
)
from app.models.artifacts_eval import Artifact
from app.models.execution import JobQueue
from app.models.governance import Approval
from app.models.observability import AuditEvent, ExecutionEvent
from app.models.tasks import AgentRun, TaskRun
from app.models.workflow import WorkflowNode, WorkflowNodeRun, WorkflowRun
from app.services.approval_service import WORKFLOW_HUMAN_APPROVAL_OPERATION
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.workflow_execution_service import WorkflowExecutionService
from tests.conftest import make_agent, make_runnable_agent_version, make_task

Built = namedtuple("Built", "workflow version published nodes agent_versions task_run project")

AGENT = "agent"
APPROVAL = "approval"
TERMINAL = "terminal"


def build_chain(db, project, layout, *, label="wf", approval_group="eng-leads", activate_agents=False):
    """A linear published workflow. ``layout`` is a list of ``(node_key, kind)``
    with kind in AGENT / APPROVAL / TERMINAL; each node is edged to the next."""
    definitions = WorkflowDefinitionService(db)
    workflow = definitions.create_workflow(project.id, f"{label}-workflow")
    version = definitions.get_latest_version(workflow.id)

    nodes, agent_versions = {}, {}
    for key, kind in layout:
        if kind == AGENT:
            agent = make_agent(db, project, f"{label}-{key}")
            agent_version = make_runnable_agent_version(db, agent)  # publishable; ``activate_agents`` kept for callers
            agent_versions[key] = agent_version
            nodes[key] = definitions.add_node(
                workflow.id,
                version.version,
                key,
                WorkflowNodeType.AGENT,
                config={"agent_version_id": agent_version.id},
            )
        elif kind == APPROVAL:
            nodes[key] = definitions.add_node(
                workflow.id,
                version.version,
                key,
                WorkflowNodeType.HUMAN_APPROVAL,
                config={"approval_group": approval_group},
            )
        else:
            nodes[key] = definitions.add_node(workflow.id, version.version, key, WorkflowNodeType.TERMINAL)

    keys = [key for key, _ in layout]
    for upstream, downstream in zip(keys, keys[1:]):
        definitions.add_edge(workflow.id, version.version, nodes[upstream].id, nodes[downstream].id)
    published = definitions.publish_version(workflow.id, version.version)

    task = make_task(db, project)
    task_run = TaskRun(task_id=task.id, status=TaskRunStatus.CREATED, created_at=datetime.now(timezone.utc))
    db.add(task_run)
    db.commit()
    return Built(workflow, version, published, nodes, agent_versions, task_run, project)


def start(db, built):
    return WorkflowExecutionService(db).start_workflow_run(built.published.id, built.task_run.id)


def install_fake_agents(monkeypatch, tmp_path, calls=None, *, content=None):
    """Stubs ONLY the inference boundary (``AgentExecutionService.execute``):
    the agent run completes and records a real artifact file with a real
    sha256. Everything downstream (worker, reconciliation, dispatch, approval
    gates) runs for real. ``calls`` collects node keys in execution order."""
    import app.services.execution_service as execution_service_module

    executed = calls if calls is not None else []

    def fake_execute(self, agent_run_id, *, worker_id):
        node_run = self.db.query(WorkflowNodeRun).filter(WorkflowNodeRun.agent_run_id == agent_run_id).first()
        node = self.db.get(WorkflowNode, node_run.workflow_node_id)
        executed.append(node.node_key)

        text = content(node.node_key) if content else f"OUTPUT-OF-{node.node_key}"
        path = tmp_path / f"artifact-{agent_run_id}.md"
        data = text.encode("utf-8")
        path.write_bytes(data)
        agent_run = self.db.get(AgentRun, agent_run_id)
        agent_run.status = AgentRunStatus.COMPLETED
        self.db.add(
            Artifact(
                agent_run_id=agent_run_id,
                type=ArtifactType.REPORT,
                storage_ref=str(path),
                content_hash=hashlib.sha256(data).hexdigest(),
            )
        )
        self.db.commit()

    monkeypatch.setattr(execution_service_module.AgentExecutionService, "execute", fake_execute)
    return executed


def drain(worker, limit=20):
    """Runs the real Worker until it finds no job. Returns jobs processed."""
    processed = 0
    while worker.run_once():
        processed += 1
        assert processed <= limit, "worker did not go idle"
    return processed


def snapshot(session_factory, run_id):
    """Fresh-session view of a workflow run (never a stale identity-map row).
    Returns a dict: run status, {node_key: node status}, approvals, counts."""
    db = session_factory()
    try:
        run = db.get(WorkflowRun, run_id)
        node_runs = db.query(WorkflowNodeRun).filter(WorkflowNodeRun.workflow_run_id == run_id).all()
        by_key = {}
        for node_run in node_runs:
            by_key[db.get(WorkflowNode, node_run.workflow_node_id).node_key] = node_run
        node_run_ids = [nr.id for nr in node_runs]
        approvals = (
            db.query(Approval)
            .filter(Approval.operation_type == WORKFLOW_HUMAN_APPROVAL_OPERATION)
            .filter(Approval.scope_ref_id.in_(node_run_ids))
            .order_by(Approval.requested_at)
            .all()
        )
        agent_run_ids = [nr.agent_run_id for nr in node_runs if nr.agent_run_id]
        return {
            "run": run.status,
            "run_ended_at": run.ended_at,
            "nodes": {key: nr.status for key, nr in by_key.items()},
            "node_runs": by_key,
            "approvals": approvals,
            "agent_run_ids": agent_run_ids,
            "agent_runs_total": db.execute(select(func.count()).select_from(AgentRun)).scalar_one(),
            "task_runs_total": db.execute(select(func.count()).select_from(TaskRun)).scalar_one(),
            "jobs_total": db.execute(select(func.count()).select_from(JobQueue)).scalar_one(),
            "agent_jobs_for_nodes": db.query(JobQueue)
            .filter(JobQueue.job_type == JobType.AGENT_RUN, JobQueue.payload_ref.in_(agent_run_ids or [""]))
            .count(),
        }
    finally:
        db.close()


def events(session_factory, task_run_id):
    db = session_factory()
    try:
        return [
            (e.event_type, e.actor_type, e.actor_user_id, e.workflow_node_run_id)
            for e in db.execute(
                select(ExecutionEvent)
                .where(ExecutionEvent.task_run_id == task_run_id)
                .order_by(ExecutionEvent.sequence_number)
            )
            .scalars()
            .all()
        ]
    finally:
        db.close()


def event_types(session_factory, task_run_id):
    return [e[0] for e in events(session_factory, task_run_id)]


def audit_types(session_factory):
    db = session_factory()
    try:
        return [e.event_type for e in db.query(AuditEvent).order_by(AuditEvent.occurred_at).all()]
    finally:
        db.close()


def resolve_via_api(client, headers, approval, *, approve=True, notes=None, fingerprint=None):
    body = {
        "approve": approve,
        "action_fingerprint": approval.action_fingerprint if fingerprint is None else fingerprint,
    }
    if notes is not None:
        body["notes"] = notes
    return client.post(f"/approvals/{approval.id}/resolve", headers=headers, json=body)

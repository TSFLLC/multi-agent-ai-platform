"""Helpers for the MA7.4a (concurrency-safety foundations) tests.

Parallel fan-out is NOT enabled in MA7.4a, so multi-parent graphs cannot be
driven by the engine end to end. These helpers build real, published
fan-out/fan-in definitions through WorkflowDefinitionService and then put a
WorkflowRun's node runs into chosen states by hand -- exactly the states a
later parallel engine (or a crash) would leave behind -- so the foundations
(fresh reads, finalize-on-drain, canonical evidence order, event isolation)
can be tested directly. Not a test module.
"""

import hashlib
from datetime import datetime, timezone

from app.db.enums import (
    AgentRunStatus,
    ArtifactType,
    TaskRunStatus,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
    WorkflowRunStatus,
)
from app.models.artifacts_eval import Artifact
from app.models.tasks import AgentRun, TaskRun
from app.models.workflow import WorkflowNodeRun, WorkflowRun
from app.services.workflow_definition_service import WorkflowDefinitionService
from tests.conftest import make_agent, make_agent_version, make_task
from tests.ma7_3b_support import Built

BRANCHES = ("test", "security", "code")


def build_diamond(db, project, *, insert_order=BRANCHES, label="dia", join_agent=True, condition_on=None):
    """eng -> {test, security, code} -> join (agent, or a Human-Approval-free
    TERMINAL when ``join_agent`` is False) -> end. ``insert_order`` is the
    order the FAN-IN edges (branch -> join) are inserted -- the thing
    canonical ordering must be independent of. ``condition_on`` names a branch
    whose eng -> branch edge gets a condition. Published unless a condition
    is present (publish must reject it -- the caller decides)."""
    definitions = WorkflowDefinitionService(db)
    workflow = definitions.create_workflow(project.id, f"{label}-workflow")
    version = definitions.get_latest_version(workflow.id)

    nodes, agent_versions = {}, {}
    agent_keys = ["eng", *BRANCHES] + (["join"] if join_agent else [])
    for key in agent_keys:
        agent_version = make_agent_version(db, make_agent(db, project, f"{label}-{key}"))
        agent_versions[key] = agent_version
        nodes[key] = definitions.add_node(
            workflow.id,
            version.version,
            key,
            WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version.id},
        )
    if join_agent:
        nodes["end"] = definitions.add_node(workflow.id, version.version, "end", WorkflowNodeType.TERMINAL)
    else:
        nodes["join"] = definitions.add_node(workflow.id, version.version, "join", WorkflowNodeType.TERMINAL)

    for branch in BRANCHES:
        definitions.add_edge(
            workflow.id,
            version.version,
            nodes["eng"].id,
            nodes[branch].id,
            condition={"when": "never"} if condition_on == branch else None,
        )
    for branch in insert_order:
        definitions.add_edge(workflow.id, version.version, nodes[branch].id, nodes["join"].id)
    if join_agent:
        definitions.add_edge(workflow.id, version.version, nodes["join"].id, nodes["end"].id)

    task = make_task(db, project)
    task_run = TaskRun(task_id=task.id, status=TaskRunStatus.CREATED, created_at=datetime.now(timezone.utc))
    db.add(task_run)
    db.commit()
    published = version if condition_on else definitions.publish_version(workflow.id, version.version)
    return Built(workflow, version, published, nodes, agent_versions, task_run, project)


def manual_run(db, built, *, status=WorkflowRunStatus.RUNNING):
    """A WorkflowRun with one PENDING node run per node -- no engine involved."""
    run = WorkflowRun(
        task_run_id=built.task_run.id,
        workflow_version_id=built.published.id,
        status=status,
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    node_runs = {}
    for key, node in built.nodes.items():
        node_runs[key] = WorkflowNodeRun(
            workflow_run_id=run.id,
            workflow_node_id=node.id,
            iteration=0,
            status=WorkflowNodeRunStatus.PENDING,
        )
        db.add(node_runs[key])
    db.commit()
    return run, node_runs


def _node_task_run(db, built):
    task_run = TaskRun(task_id=built.task_run.task_id, status=TaskRunStatus.RUNNING)
    db.add(task_run)
    db.flush()
    return task_run


def complete_node(db, built, node_runs, key, *, text=None, tmp_path=None, artifact_id=None):
    """Node ``key`` COMPLETED with an output artifact. With ``text`` the
    artifact is backed by a real file and sha256; ``artifact_id`` instead
    re-uses an existing artifact (duplicate-artifact scenarios). Returns the
    output artifact id."""
    agent_run = AgentRun(
        task_run_id=_node_task_run(db, built).id,
        agent_version_id=built.agent_versions[key].id
        if key in built.agent_versions
        else next(iter(built.agent_versions.values())).id,
        status=AgentRunStatus.COMPLETED,
    )
    db.add(agent_run)
    db.flush()
    if artifact_id is None:
        if text is not None:
            path = tmp_path / f"{key}-{agent_run.id}.md"
            data = text.encode("utf-8")
            path.write_bytes(data)
            artifact = Artifact(
                agent_run_id=agent_run.id,
                type=ArtifactType.REPORT,
                storage_ref=str(path),
                content_hash=hashlib.sha256(data).hexdigest(),
            )
        else:
            artifact = Artifact(
                agent_run_id=agent_run.id,
                type=ArtifactType.REPORT,
                storage_ref=f"/none/{key}",
                content_hash=None,
            )
        db.add(artifact)
        db.flush()
        artifact_id = artifact.id
    node_run = node_runs[key]
    node_run.status = WorkflowNodeRunStatus.COMPLETED
    node_run.agent_run_id = agent_run.id
    node_run.output_snapshot_ref = artifact_id
    node_run.ended_at = datetime.now(timezone.utc)
    db.commit()
    return artifact_id


def run_node(db, built, node_runs, key):
    """Node ``key`` RUNNING with a RUNNING AgentRun (in flight). Returns the AgentRun."""
    agent_run = AgentRun(
        task_run_id=_node_task_run(db, built).id,
        agent_version_id=built.agent_versions[key].id
        if key in built.agent_versions
        else next(iter(built.agent_versions.values())).id,
        status=AgentRunStatus.RUNNING,
    )
    db.add(agent_run)
    db.flush()
    node_runs[key].status = WorkflowNodeRunStatus.RUNNING
    node_runs[key].agent_run_id = agent_run.id
    db.commit()
    return agent_run


def finish_agent(db, agent_run, status, *, text=None, tmp_path=None):
    """The in-flight agent reaches a terminal status (the moment a worker
    would call ``on_agent_run_complete``); COMPLETED gets a real artifact."""
    agent_run.status = status
    if status == AgentRunStatus.COMPLETED and text is not None:
        path = tmp_path / f"out-{agent_run.id}.md"
        data = text.encode("utf-8")
        path.write_bytes(data)
        db.add(
            Artifact(
                agent_run_id=agent_run.id,
                type=ArtifactType.REPORT,
                storage_ref=str(path),
                content_hash=hashlib.sha256(data).hexdigest(),
            )
        )
    db.commit()

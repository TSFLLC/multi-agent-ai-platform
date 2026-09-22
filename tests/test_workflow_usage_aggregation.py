"""build_run_detail's workflow-level usage aggregate -- MA7.7D fix.

A retried node's pre-retry failed attempt keeps its AgentRun/ModelCall
rows (Section 24.4 #10 -- history is never deleted), but until this fix
build_run_detail's run-level ``usage`` total only summed each node's
LATEST iteration, silently dropping real spend on every failed-then-
retried attempt. This test builds the minimal DB rows directly (no
engine/worker dispatch needed -- this is a pure read-service concern)
and proves the total now includes both iterations.
"""

from decimal import Decimal

from app.db.enums import (
    AgentRunStatus,
    VersionStatus,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
    WorkflowRunStatus,
)
from app.models.execution import ModelCall
from app.models.workflow import Workflow, WorkflowNode, WorkflowNodeRun, WorkflowRun, WorkflowVersion
from app.services.workflow_run_read_service import build_run_detail
from tests.conftest import (
    make_agent,
    make_agent_run,
    make_agent_version,
    make_model,
    make_project,
    make_provider,
    make_task,
    make_task_run,
)


def _model_call(db, *, agent_run, model, provider, tokens_in, tokens_out):
    call = ModelCall(
        agent_run_id=agent_run.id,
        model_id=model.id,
        provider_id=provider.id,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_amount=Decimal("0.01"),
        cost_currency="USD",
        cost_is_estimated=True,
        status="success",
    )
    db.add(call)
    db.flush()
    return call


def test_run_level_usage_includes_a_retried_nodes_pre_retry_failed_attempt(db):
    project = make_project(db)
    agent = make_agent(db, project=project)
    agent_version = make_agent_version(db, agent=agent, version=1, status=VersionStatus.ACTIVE)
    model = make_model(db)
    provider = make_provider(db)

    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task)

    workflow = Workflow(project_id=project.id, name="Usage Aggregation Test")
    db.add(workflow)
    db.flush()
    version = WorkflowVersion(workflow_id=workflow.id, version=1, status=VersionStatus.ACTIVE)
    db.add(version)
    db.flush()
    node = WorkflowNode(
        workflow_version_id=version.id,
        node_key="engineer",
        node_type=WorkflowNodeType.AGENT,
        config={"agent_version_id": agent_version.id},
    )
    db.add(node)
    db.flush()

    run = WorkflowRun(
        task_run_id=task_run.id, workflow_version_id=version.id, status=WorkflowRunStatus.RUNNING
    )
    db.add(run)
    db.flush()

    failed_agent_run = make_agent_run(
        db, task_run=task_run, agent_version=agent_version, status=AgentRunStatus.FAILED
    )
    failed_agent_run.model_id = model.id
    failed_agent_run.provider_id = provider.id
    db.flush()
    _model_call(db, agent_run=failed_agent_run, model=model, provider=provider, tokens_in=100, tokens_out=50)

    retried_agent_run = make_agent_run(
        db, task_run=task_run, agent_version=agent_version, status=AgentRunStatus.COMPLETED
    )
    retried_agent_run.model_id = model.id
    retried_agent_run.provider_id = provider.id
    db.flush()
    _model_call(db, agent_run=retried_agent_run, model=model, provider=provider, tokens_in=20, tokens_out=10)

    db.add(
        WorkflowNodeRun(
            workflow_run_id=run.id,
            workflow_node_id=node.id,
            iteration=0,
            status=WorkflowNodeRunStatus.FAILED,
            agent_run_id=failed_agent_run.id,
        )
    )
    db.add(
        WorkflowNodeRun(
            workflow_run_id=run.id,
            workflow_node_id=node.id,
            iteration=1,
            status=WorkflowNodeRunStatus.COMPLETED,
            agent_run_id=retried_agent_run.id,
        )
    )
    db.commit()

    detail = build_run_detail(db, run)

    # The bug: only the latest iteration's 20+10=30 tokens would show.
    # The fix: both iterations' spend is counted -- (100+50) + (20+10).
    assert detail.usage.tokens_in == 120
    assert detail.usage.tokens_out == 60
    assert detail.usage.total_tokens == 180
    assert detail.usage.model_call_count == 2

    # Per-node display still reflects only the CURRENT (latest) attempt --
    # this fix must not change that, only the run-level total.
    assert len(detail.nodes) == 1
    assert detail.nodes[0].iteration == 1
    assert detail.nodes[0].usage.total_tokens == 30

"""WorkflowRun read service -- the Control Room's data (MA7.6B).

Assembles read-only snapshots from rows that already exist. It writes nothing,
persists no aggregate, and invents no state: node status is the node run's own
status, an Agent's model is the model its Agent Run actually resolved, and usage
and cost are summed from ``model_calls`` (the same evidence the MA6 evaluation
read model uses), by Agent Run -> WorkflowNodeRun -> WorkflowRun.

Cost arithmetic is done in ``Decimal`` in Python (never a SQL ``SUM``, which
SQLite returns as a float for a Numeric column), and a total that includes any
estimated call says so.
"""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import (
    AgentRunStatus,
    ApprovalScope,
    WorkflowNodeRunStatus,
    WorkflowRunStatus,
)
from app.models.agents import Agent, AgentVersion
from app.models.evaluation_runs import EvaluationRun
from app.models.execution import ModelCall
from app.models.governance import Approval
from app.models.observability import ExecutionEvent
from app.models.providers import Model, Provider
from app.models.tasks import AgentRun, AgentRunAttempt, Task, TaskRun
from app.models.workflow import (
    Workflow,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.schemas.workflow_runs import (
    RunAgentRead,
    RunEdgeRead,
    RunFailureRead,
    RunModelRead,
    RunNodeRead,
    RunUsageRead,
    WorkflowRunDetailRead,
    WorkflowRunSummaryRead,
)
from app.services.approval_service import WORKFLOW_HUMAN_APPROVAL_OPERATION

MAX_RUN_HISTORY = 200


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal(0)
    return value if isinstance(value, Decimal) else Decimal(str(value))


def aggregate_usage(calls: Iterable[ModelCall]) -> Optional[RunUsageRead]:
    """Sums tokens and cost over ``calls``; ``None`` when there are none (never
    presented as zeros). A call that recorded no tokens/cost (a failed call)
    contributes nothing but is still counted."""
    calls = list(calls)
    if not calls:
        return None
    tokens_in = sum(call.tokens_in or 0 for call in calls)
    tokens_out = sum(call.tokens_out or 0 for call in calls)
    currencies = {call.cost_currency for call in calls}
    if len(currencies) == 1:
        currency = next(iter(currencies))
        amount: Optional[Decimal] = sum((_decimal(call.cost_amount) for call in calls), Decimal(0))
    else:
        currency, amount = "mixed", None  # amounts in different currencies are never added
    return RunUsageRead(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        total_tokens=tokens_in + tokens_out,
        cost_amount=amount,
        cost_currency=currency,
        cost_is_estimated=any(call.cost_is_estimated for call in calls),
        model_call_count=len(calls),
    )


def _duration_seconds(started: Optional[datetime], ended: Optional[datetime]) -> Optional[float]:
    if started is None or ended is None:
        return None
    try:
        return max(0.0, (ended - started).total_seconds())
    except TypeError:  # naive vs aware: never guess
        return None


def _latest_node_runs(node_runs: Sequence[WorkflowNodeRun]) -> Dict[str, WorkflowNodeRun]:
    latest: Dict[str, WorkflowNodeRun] = {}
    for node_run in node_runs:
        current = latest.get(node_run.workflow_node_id)
        if current is None or node_run.iteration > current.iteration:
            latest[node_run.workflow_node_id] = node_run
    return latest


def _calls_by_agent_run(db: Session, agent_run_ids: Sequence[str]) -> Dict[str, List[ModelCall]]:
    grouped: Dict[str, List[ModelCall]] = defaultdict(list)
    if agent_run_ids:
        for call in db.execute(
            select(ModelCall).where(ModelCall.agent_run_id.in_(list(agent_run_ids)))
        ).scalars():
            grouped[call.agent_run_id].append(call)
    return grouped


def build_run_detail(db: Session, run: WorkflowRun) -> WorkflowRunDetailRead:
    version = db.get(WorkflowVersion, run.workflow_version_id)
    workflow = db.get(Workflow, version.workflow_id) if version else None
    task_run = db.get(TaskRun, run.task_run_id)
    task = db.get(Task, task_run.task_id) if task_run else None

    nodes = sorted(
        db.execute(
            select(WorkflowNode).where(WorkflowNode.workflow_version_id == run.workflow_version_id)
        ).scalars(),
        key=lambda node: node.node_key,
    )
    edges = list(
        db.execute(
            select(WorkflowEdge).where(WorkflowEdge.workflow_version_id == run.workflow_version_id)
        ).scalars()
    )
    node_runs = list(
        db.execute(select(WorkflowNodeRun).where(WorkflowNodeRun.workflow_run_id == run.id)).scalars()
    )
    latest = _latest_node_runs(node_runs)

    # MA7.7D fix: the run-level usage TOTAL must include every iteration's
    # cost/tokens, not just each node's current (latest) attempt -- a
    # retried node's pre-retry failed attempt already spent real money
    # (Section 24.4 #10 preserves its AgentRun/ModelCall rows), and
    # "retries accounted as separate attempts without losing previous
    # usage" means that spend must still show up in the run's total, even
    # though the per-node display below intentionally shows only the
    # current/latest attempt's own state.
    all_iteration_agent_run_ids = [nr.agent_run_id for nr in node_runs if nr.agent_run_id]
    all_iteration_calls = _calls_by_agent_run(db, all_iteration_agent_run_ids)

    agent_run_ids = [nr.agent_run_id for nr in latest.values() if nr.agent_run_id]
    node_run_ids = [nr.id for nr in latest.values()]
    agent_runs = (
        {ar.id: ar for ar in db.execute(select(AgentRun).where(AgentRun.id.in_(agent_run_ids))).scalars()}
        if agent_run_ids
        else {}
    )
    versions = (
        {
            av.id: av
            for av in db.execute(
                select(AgentVersion).where(
                    AgentVersion.id.in_({ar.agent_version_id for ar in agent_runs.values()})
                )
            ).scalars()
        }
        if agent_runs
        else {}
    )
    agents = (
        {
            a.id: a
            for a in db.execute(
                select(Agent).where(Agent.id.in_({v.agent_id for v in versions.values()}))
            ).scalars()
        }
        if versions
        else {}
    )
    models = (
        {
            m.id: m
            for m in db.execute(
                select(Model).where(Model.id.in_({ar.model_id for ar in agent_runs.values() if ar.model_id}))
            ).scalars()
        }
        if agent_runs
        else {}
    )
    providers = (
        {
            p.id: p
            for p in db.execute(
                select(Provider).where(
                    Provider.id.in_({ar.provider_id for ar in agent_runs.values() if ar.provider_id})
                )
            ).scalars()
        }
        if agent_runs
        else {}
    )

    evaluations = (
        {
            e.evaluator_agent_run_id: e
            for e in db.execute(
                select(EvaluationRun).where(EvaluationRun.evaluator_agent_run_id.in_(agent_run_ids))
            ).scalars()
        }
        if agent_run_ids
        else {}
    )
    approvals = (
        {
            a.scope_ref_id: a
            for a in db.execute(
                select(Approval).where(
                    Approval.scope == ApprovalScope.WORKFLOW_NODE_RUN,
                    Approval.operation_type == WORKFLOW_HUMAN_APPROVAL_OPERATION,
                    Approval.scope_ref_id.in_(node_run_ids),
                )
            ).scalars()
        }
        if node_run_ids
        else {}
    )
    calls = _calls_by_agent_run(db, agent_run_ids)

    # The recorded error of each failed Agent Run's latest attempt, and dispatch-time node failures.
    failed_agent_ids = [i for i in agent_run_ids if agent_runs[i].status == AgentRunStatus.FAILED]
    attempt_error: Dict[str, Dict[str, Any]] = {}
    if failed_agent_ids:
        for attempt in db.execute(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.agent_run_id.in_(failed_agent_ids))
            .order_by(AgentRunAttempt.agent_run_id, AgentRunAttempt.attempt_number)
        ).scalars():
            if attempt.error:
                attempt_error[attempt.agent_run_id] = attempt.error  # ascending: the last one wins
    failed_node_run_ids = [nr.id for nr in latest.values() if nr.status == WorkflowNodeRunStatus.FAILED]
    node_event_error: Dict[str, Dict[str, Any]] = {}
    if failed_node_run_ids:
        for event in db.execute(
            select(ExecutionEvent)
            .where(
                ExecutionEvent.workflow_node_run_id.in_(failed_node_run_ids),
                ExecutionEvent.event_type == "workflow.node.failed",
            )
            .order_by(ExecutionEvent.sequence_number)
        ).scalars():
            if event.error and event.workflow_node_run_id:
                node_event_error[event.workflow_node_run_id] = event.error

    # MA7.8B: which explicit operator retry each failed step offers -- decided by the engine's own rules
    # (never a UI guess), and none at all while the run could not be resumed afterwards.
    retry_modes: Dict[str, Optional[str]] = {}
    if run.status == WorkflowRunStatus.FAILED and failed_node_run_ids:
        from app.services.workflow_execution_service import WorkflowExecutionService

        engine = WorkflowExecutionService(db)
        if not engine._fail_fast_cancellation_plan(run).blocking:
            nodes_by_id = {node.id: node for node in nodes}
            for failed in (nr for nr in latest.values() if nr.status == WorkflowNodeRunStatus.FAILED):
                retry_modes[failed.id] = engine.retry_mode(failed, nodes_by_id.get(failed.workflow_node_id))[0]

    node_reads: List[RunNodeRead] = []
    for node in nodes:
        node_run = latest.get(node.id)
        agent_run = agent_runs.get(node_run.agent_run_id) if node_run and node_run.agent_run_id else None
        agent_read: Optional[RunAgentRead] = None
        usage: Optional[RunUsageRead] = None
        failure: Optional[RunFailureRead] = None
        evaluation = evaluations.get(agent_run.id) if agent_run else None
        if agent_run is not None:
            agent_version = versions.get(agent_run.agent_version_id)
            agent = agents.get(agent_version.agent_id) if agent_version else None
            model = models.get(agent_run.model_id) if agent_run.model_id else None
            provider = providers.get(agent_run.provider_id) if agent_run.provider_id else None
            agent_read = RunAgentRead(
                agent_run_id=agent_run.id,
                status=agent_run.status,
                run_role=agent_run.role,
                agent_id=agent.id if agent else None,
                agent_name=agent.name if agent else None,
                agent_version_id=agent_run.agent_version_id,
                agent_version=agent_version.version if agent_version else None,
                role=agent_version.role if agent_version else None,
                model=(
                    RunModelRead(
                        model_id=agent_run.model_id,
                        canonical_model_id=model.canonical_model_id if model else None,
                        provider_id=agent_run.provider_id,
                        provider_name=provider.name if provider else None,
                        provider_model_snapshot_id=agent_run.provider_model_snapshot_id,
                    )
                    if agent_run.model_id or agent_run.provider_id
                    else None
                ),
                started_at=agent_run.started_at,
                ended_at=agent_run.ended_at,
            )
            usage = aggregate_usage(calls.get(agent_run.id, []))
        if node_run is not None and node_run.status == WorkflowNodeRunStatus.FAILED:
            if agent_run is not None and agent_run.id in attempt_error:
                error = attempt_error[agent_run.id]
                failure = RunFailureRead(
                    category=error.get("category"),
                    message=str(error.get("message") or ""),
                    source="agent_run",
                )
            elif evaluation is not None and evaluation.failure_reason:
                failure = RunFailureRead(
                    category=None, message=evaluation.failure_reason, source="evaluation"
                )
            elif str((node_event_error.get(node_run.id) or {}).get("message") or ""):
                # (a gate a human rejected also emits this event, without a message: that is a decision, not an error)
                failure = RunFailureRead(
                    category=None, message=str(node_event_error[node_run.id]["message"]), source="workflow"
                )
        approval = approvals.get(node_run.id) if node_run else None
        # The engine records a node run's end, but its START is recorded on the Agent Run that executes it:
        # use whichever the backend actually has (never a made-up time).
        started_at = (node_run.started_at if node_run else None) or (
            agent_run.started_at if agent_run else None
        )
        ended_at = (node_run.ended_at if node_run else None) or (agent_run.ended_at if agent_run else None)
        node_reads.append(
            RunNodeRead(
                node_id=node.id,
                node_key=node.node_key,
                node_type=node.node_type,
                node_run_id=node_run.id if node_run else None,
                iteration=node_run.iteration if node_run else 0,
                status=node_run.status if node_run else WorkflowNodeRunStatus.PENDING,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=_duration_seconds(started_at, ended_at),
                agent=agent_read,
                output_artifact_id=node_run.output_snapshot_ref if node_run else None,
                evaluation_run_id=evaluation.id if evaluation else None,
                evaluation_status=evaluation.status if evaluation else None,
                approval_id=approval.id if approval else None,
                approval_status=approval.status if approval else None,
                usage=usage,
                failure=failure,
                retry_mode=retry_modes.get(node_run.id) if node_run else None,
            )
        )

    all_calls = [call for group in all_iteration_calls.values() for call in group]
    return WorkflowRunDetailRead(
        id=run.id,
        status=run.status,
        workflow_id=workflow.id if workflow else "",
        workflow_name=workflow.name if workflow else "",
        workflow_version_id=run.workflow_version_id,
        workflow_version=version.version if version else 0,
        workflow_version_status=version.status if version else None,
        task_run_id=run.task_run_id,
        task_id=task_run.task_id if task_run else None,
        assignment_title=task.title if task else None,
        created_at=run.created_at,
        started_at=run.started_at,
        ended_at=run.ended_at,
        cancellation_requested_at=run.cancellation_requested_at,
        nodes=node_reads,
        edges=[RunEdgeRead(id=e.id, from_node_id=e.from_node_id, to_node_id=e.to_node_id) for e in edges],
        usage=aggregate_usage(all_calls),
    )


def list_runs_for_workflow(db: Session, workflow_id: str, limit: int = 50) -> List[WorkflowRunSummaryRead]:
    """A workflow's run history across ALL of its versions, newest first. Purely
    factual: status, timing, the version each run is bound to, and usage when it
    exists -- never a ranking or a performance score."""
    limit = max(1, min(int(limit), MAX_RUN_HISTORY))
    rows = db.execute(
        select(WorkflowRun, WorkflowVersion.version)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
        .where(WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .limit(limit)
    ).all()
    if not rows:
        return []
    runs = [row[0] for row in rows]
    task_runs = {
        tr.id: tr
        for tr in db.execute(select(TaskRun).where(TaskRun.id.in_([r.task_run_id for r in runs]))).scalars()
    }
    tasks = (
        {
            t.id: t
            for t in db.execute(
                select(Task).where(Task.id.in_({tr.task_id for tr in task_runs.values()}))
            ).scalars()
        }
        if task_runs
        else {}
    )
    agent_ids_by_run: Dict[str, List[str]] = defaultdict(list)
    for run_id, agent_run_id in db.execute(
        select(WorkflowNodeRun.workflow_run_id, WorkflowNodeRun.agent_run_id).where(
            WorkflowNodeRun.workflow_run_id.in_([r.id for r in runs]),
            WorkflowNodeRun.agent_run_id.isnot(None),
        )
    ).all():
        agent_ids_by_run[run_id].append(agent_run_id)
    all_agent_ids = [i for ids in agent_ids_by_run.values() for i in ids]
    calls = _calls_by_agent_run(db, all_agent_ids)

    summaries: List[WorkflowRunSummaryRead] = []
    for run, version_number in rows:
        task_run = task_runs.get(run.task_run_id)
        task = tasks.get(task_run.task_id) if task_run else None
        run_calls = [
            call for agent_id in agent_ids_by_run.get(run.id, []) for call in calls.get(agent_id, [])
        ]
        summaries.append(
            WorkflowRunSummaryRead(
                id=run.id,
                status=run.status,
                workflow_version_id=run.workflow_version_id,
                workflow_version=version_number,
                task_run_id=run.task_run_id,
                assignment_title=task.title if task else None,
                created_at=run.created_at,
                started_at=run.started_at,
                ended_at=run.ended_at,
                usage=aggregate_usage(run_calls),
            )
        )
    return summaries

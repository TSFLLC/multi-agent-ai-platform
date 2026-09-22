"""WorkflowRun read models for the operator Control Room (MA7.6B).

Read-only views assembled from existing rows -- nothing here is persisted and no
column, status or aggregate is invented:

* runtime state is the backend's own ``WorkflowRunStatus`` /
  ``WorkflowNodeRunStatus`` / ``AgentRunStatus`` / ``ApprovalStatus``;
* usage and cost are derived from ``model_calls`` (never copied anywhere);
* there is no score, rank, winner, verdict or recommendation field, and no
  aggregate over evaluation findings -- an Evaluation stays evidence, read through
  ``GET /evaluation-runs/{id}``.
"""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel

from app.db.enums import (
    AgentRunRole,
    AgentRunStatus,
    ApprovalStatus,
    EvaluationRunStatus,
    VersionStatus,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
    WorkflowRunStatus,
)


class RunUsageRead(BaseModel):
    """Tokens and cost summed from the ``model_calls`` of one Agent Run, or of
    every Agent Run of a WorkflowRun. ``None`` on the parent (never zeros) when no
    model call exists yet.

    ``cost_is_estimated`` is True when ANY included call's cost is an estimate
    rather than provider-reported/verified-free, so a total that mixes the two is
    never presented as exact. ``cost_amount`` is ``None`` only if the calls span
    more than one currency (then ``cost_currency`` is ``"mixed"``): amounts in
    different currencies are never added."""

    tokens_in: int
    tokens_out: int
    total_tokens: int
    cost_amount: Optional[Decimal] = None
    cost_currency: str
    cost_is_estimated: bool
    model_call_count: int


class RunModelRead(BaseModel):
    """The model an Agent Run actually resolved (from the run's own
    model/provider/snapshot FKs). Separate from the Agent: Agent != Model."""

    model_id: Optional[str] = None
    canonical_model_id: Optional[str] = None
    provider_id: Optional[str] = None
    provider_name: Optional[str] = None
    provider_model_snapshot_id: Optional[str] = None


class RunAgentRead(BaseModel):
    agent_run_id: str
    status: AgentRunStatus
    # MA6 tag: EVALUATOR for an Evaluation node's evaluator run, else unset.
    run_role: Optional[AgentRunRole] = None
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_version_id: str
    agent_version: Optional[int] = None
    role: Optional[str] = None
    model: Optional[RunModelRead] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class RunFailureRead(BaseModel):
    """Why a node failed, as recorded -- categories and messages only, never
    prompt, artifact or credential content."""

    category: Optional[str] = None
    message: str
    # "agent_run" (the attempt's recorded error), "evaluation" (the EvaluationRun's failure reason) or
    # "workflow" (a dispatch-time node failure)
    source: str


class RunNodeRead(BaseModel):
    node_id: str
    node_key: str
    node_type: WorkflowNodeType
    node_run_id: Optional[str] = None
    iteration: int = 0
    status: WorkflowNodeRunStatus
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    agent: Optional[RunAgentRead] = None
    output_artifact_id: Optional[str] = None
    evaluation_run_id: Optional[str] = None
    evaluation_status: Optional[EvaluationRunStatus] = None
    approval_id: Optional[str] = None
    approval_status: Optional[ApprovalStatus] = None
    usage: Optional[RunUsageRead] = None
    failure: Optional[RunFailureRead] = None
    # MA7.8B: the explicit retry this failed step offers -- "as_configured" (re-run with its own
    # configuration), "replacement" (MA7.6B: choose a replacement model) or None.
    retry_mode: Optional[str] = None


class RunEdgeRead(BaseModel):
    id: str
    from_node_id: str
    to_node_id: str


class WorkflowRunDetailRead(BaseModel):
    """One consistent snapshot of a run: the exact bound version's graph plus
    every node's runtime state. Polled by the Control Room."""

    id: str
    status: WorkflowRunStatus
    workflow_id: str
    workflow_name: str
    workflow_version_id: str
    workflow_version: int
    workflow_version_status: Optional[VersionStatus] = None
    task_run_id: str
    task_id: Optional[str] = None
    assignment_title: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    cancellation_requested_at: Optional[datetime] = None
    nodes: List[RunNodeRead]
    edges: List[RunEdgeRead]
    usage: Optional[RunUsageRead] = None


class WorkflowRunSummaryRead(BaseModel):
    """A row of a workflow's run history (all versions)."""

    id: str
    status: WorkflowRunStatus
    workflow_version_id: str
    workflow_version: int
    task_run_id: str
    assignment_title: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    usage: Optional[RunUsageRead] = None

from typing import List, Optional

from pydantic import BaseModel

from app.db.enums import VersionStatus, WorkflowNodeRunStatus, WorkflowNodeType, WorkflowRunStatus


class WorkflowCreate(BaseModel):
    project_id: str
    name: str


class WorkflowRead(BaseModel):
    id: str
    project_id: str
    name: str
    current_status: Optional[VersionStatus] = None


class WorkflowNodeCreate(BaseModel):
    node_key: str
    node_type: WorkflowNodeType
    config: Optional[dict] = None
    max_iterations: Optional[int] = None


class WorkflowNodeUpdate(BaseModel):
    """PATCH body for a node of a DRAFT version. Only ``config`` is updatable:
    the domain service exposes nothing else (a node's key and type are fixed
    once created)."""

    config: Optional[dict] = None


class WorkflowValidationRead(BaseModel):
    """Dry-run validation result. ``issues`` are the validator's own messages,
    one per problem, in its order -- textual, so a client must not assume they
    map to exactly one node."""

    valid: bool
    issues: List[str] = []


class WorkflowRunStart(BaseModel):
    """Start body: exactly one of ``task_run_id`` (an existing parent TaskRun)
    or ``task_id`` (the run's parent TaskRun is created with the run)."""

    task_run_id: Optional[str] = None
    task_id: Optional[str] = None


class WorkflowVersionRead(BaseModel):
    id: str
    workflow_id: str
    version: int
    status: VersionStatus


class WorkflowRunRead(BaseModel):
    id: str
    task_run_id: str
    workflow_version_id: str
    status: WorkflowRunStatus


class WorkflowNodeRunRead(BaseModel):
    id: str
    workflow_run_id: str
    workflow_node_id: str
    iteration: int
    status: WorkflowNodeRunStatus
    agent_run_id: Optional[str] = None
    # MA7.3b: the Approval a HUMAN_APPROVAL node run is waiting on / was
    # resolved by (None for every other node run).
    approval_id: Optional[str] = None
    # MA7.5A: the MA6 EvaluationRun an EVALUATION node run executes (None for
    # every other node run). Derived -- never stored on the node run: it is
    # the EvaluationRun whose UNIQUE evaluator_agent_run_id is this node run's
    # agent_run_id. Read the findings via GET /evaluation-runs/{id}.
    evaluation_run_id: Optional[str] = None


class WorkflowNodeRetryRequest(BaseModel):
    """MA7.6B: POST /workflow-runs/{run_id}/nodes/{node_run_id}/retry body --
    a run/attempt-level model override only, never a WorkflowVersion edit."""

    replacement_provider_model_id: str

from typing import Optional

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

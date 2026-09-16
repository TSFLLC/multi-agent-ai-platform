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

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.db.enums import AgentRunRole, AgentRunStatus, ExecutionMode, TaskRunStatus, TaskStatus


class TaskCreate(BaseModel):
    project_id: str
    title: str
    description: Optional[str] = None
    execution_mode: ExecutionMode
    requirements: Optional[dict] = None


class TaskRead(BaseModel):
    id: str
    project_id: str
    title: str
    execution_mode: ExecutionMode
    status: TaskStatus


class TaskRunCreate(BaseModel):
    """The Agent Version to run is selected per Task Run, not stored on
    the Task itself — a Task is a reusable definition (Section 26.1) that
    may, in principle, be run by different Agent Versions over time."""

    agent_version_id: str
    budget_id: Optional[str] = None


class TaskRunRead(BaseModel):
    id: str
    task_id: str
    status: TaskRunStatus
    config_snapshot: Optional[dict] = None
    final_artifact_id: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    cancellation_requested_at: Optional[datetime] = None


class AgentRunRead(BaseModel):
    id: str
    task_run_id: str
    agent_version_id: str
    status: AgentRunStatus
    role: Optional[AgentRunRole] = None
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    provider_model_snapshot_id: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class AgentRunAttemptRead(BaseModel):
    id: str
    agent_run_id: str
    attempt_number: int
    status: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    error: Optional[dict] = None
    worker_id: Optional[str] = None

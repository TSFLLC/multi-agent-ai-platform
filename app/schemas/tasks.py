from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.db.enums import AgentRunStatus, ExecutionMode, TaskRunStatus, TaskStatus


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


class TaskRunRead(BaseModel):
    id: str
    task_id: str
    status: TaskRunStatus
    config_snapshot: Optional[dict] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class AgentRunRead(BaseModel):
    id: str
    task_run_id: str
    agent_version_id: str
    status: AgentRunStatus
    model_id: Optional[str] = None
    provider_id: Optional[str] = None


class AgentRunAttemptRead(BaseModel):
    id: str
    agent_run_id: str
    attempt_number: int
    status: str

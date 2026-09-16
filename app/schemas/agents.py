from typing import List, Optional

from pydantic import BaseModel

from app.db.enums import DefaultModelStrategy, ToolGrantType, ToolStatus, VersionStatus
from app.schemas.common import TimestampedRead


class AgentCreate(BaseModel):
    project_id: str
    name: str
    role: str
    description: Optional[str] = None


class AgentRead(TimestampedRead):
    project_id: str
    name: str
    role: str
    current_status: Optional[VersionStatus] = None


class PromptVersionCreate(BaseModel):
    content: str


class PromptVersionRead(BaseModel):
    id: str
    agent_id: str
    version: int
    content: str


class AgentVersionCreate(BaseModel):
    name: str
    role: str
    description: Optional[str] = None
    prompt_version_id: Optional[str] = None
    capabilities: Optional[List[str]] = None
    model_policy: Optional[dict] = None
    default_model_strategy: DefaultModelStrategy = DefaultModelStrategy.MANUAL_REQUIRED
    timeout_seconds: Optional[int] = None


class AgentVersionRead(BaseModel):
    id: str
    agent_id: str
    version: int
    name: str
    role: str
    status: VersionStatus


class ToolGrantCreate(BaseModel):
    tool_id: str
    grant_type: ToolGrantType


class ToolRead(BaseModel):
    id: str
    name: str
    category: str
    status: ToolStatus

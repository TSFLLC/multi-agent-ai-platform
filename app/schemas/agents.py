from typing import List, Optional

from pydantic import BaseModel, model_validator

from app.db.enums import (
    DefaultModelStrategy,
    ModelSelectionMode,
    RouterFreePolicy,
    ToolGrantType,
    ToolStatus,
    VersionStatus,
)
from app.schemas.common import ORMModel, TimestampedRead


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


class PromptVersionRead(ORMModel):
    id: str
    agent_id: str
    version: int
    content: str


class ModelPolicy(BaseModel):
    """Section 10/14 — Owner's FREE_ONLY/PREFER_FREE/ANY requirement, made
    concrete as the shape stored in ``agent_versions.model_policy`` (JSON;
    Section 10.5.5 — a preference/constraint snapshot, never a stored FK
    to a model, per ADR-1). MA2 wires manual selection end-to-end; the
    auto policies are represented but not evaluated by any router yet
    (that's MA8) — a later Router only has to *read* this shape, not wait
    for a schema change.
    """

    mode: ModelSelectionMode = ModelSelectionMode.MANUAL
    manual_provider_model_id: Optional[str] = None
    auto_policy: Optional[RouterFreePolicy] = None

    @model_validator(mode="after")
    def _require_fields_for_mode(self) -> "ModelPolicy":
        if self.mode == ModelSelectionMode.MANUAL and not self.manual_provider_model_id:
            raise ValueError("manual_provider_model_id is required when mode='manual'")
        if self.mode == ModelSelectionMode.AUTO and self.auto_policy is None:
            raise ValueError("auto_policy is required when mode='auto'")
        return self


class AgentVersionCreate(BaseModel):
    name: str
    role: str
    description: Optional[str] = None
    prompt_version_id: Optional[str] = None
    capabilities: Optional[List[str]] = None
    model_policy: Optional[ModelPolicy] = None
    default_model_strategy: DefaultModelStrategy = DefaultModelStrategy.MANUAL_REQUIRED
    timeout_seconds: Optional[int] = None


class AgentVersionRead(ORMModel):
    id: str
    agent_id: str
    version: int
    name: str
    role: str
    description: Optional[str] = None
    prompt_version_id: Optional[str] = None
    model_policy: Optional[dict] = None
    default_model_strategy: DefaultModelStrategy
    status: VersionStatus


class ToolGrantCreate(BaseModel):
    tool_id: str
    grant_type: ToolGrantType


class ToolRead(ORMModel):
    id: str
    name: str
    category: str
    status: ToolStatus

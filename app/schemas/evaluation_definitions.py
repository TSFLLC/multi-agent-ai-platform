"""Evaluation Definition Registry contracts — MA6 Slice 1.

Mirrors app.schemas.agents's shape (AgentCreate/AgentRead/AgentVersionCreate/
AgentVersionRead) applied to evaluation rubrics instead of Agents. No
result/finding/score shape exists here on purpose -- Slice 1 stops at
"what is measured and how it's versioned," never "what a criterion's
result was" (EvaluationCriterionResult is a later slice).
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, model_validator

from app.db.enums import VersionStatus
from app.schemas.common import ORMModel, TimestampedRead


class EvaluationDefinitionCreate(BaseModel):
    project_id: str
    name: str
    description: Optional[str] = None


class EvaluationDefinitionRead(TimestampedRead):
    project_id: str
    name: str
    description: Optional[str] = None
    current_status: Optional[VersionStatus] = None


class EvaluationCriterionCreate(BaseModel):
    key: str
    label: str
    description: Optional[str] = None
    # Free-text, unenforced in Slice 1 (see app.models.evaluation_definitions
    # module docstring) -- e.g. "deterministic" / "agent_judge" / "human".
    method_hint: Optional[str] = None


class EvaluationCriterionRead(ORMModel):
    id: str
    key: str
    label: str
    description: Optional[str] = None
    method_hint: Optional[str] = None
    order_index: int


class EvaluationDefinitionVersionCreate(BaseModel):
    description: Optional[str] = None
    criteria: List[EvaluationCriterionCreate]

    @model_validator(mode="after")
    def _require_at_least_one_criterion_with_unique_keys(self) -> "EvaluationDefinitionVersionCreate":
        if not self.criteria:
            raise ValueError("A rubric version requires at least one criterion.")
        keys = [c.key for c in self.criteria]
        if len(set(keys)) != len(keys):
            raise ValueError("Criterion keys must be unique within one rubric version.")
        return self


class EvaluationDefinitionVersionRead(ORMModel):
    id: str
    evaluation_definition_id: str
    version: int
    description: Optional[str] = None
    status: VersionStatus
    created_at: datetime
    published_at: Optional[datetime] = None
    criteria: List[EvaluationCriterionRead] = []

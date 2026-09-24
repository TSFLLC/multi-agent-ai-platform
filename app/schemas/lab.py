from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field

from app.db.enums import (
    ConclusionType,
    CostEstimateKind,
    EvalSetVersionStatus,
    EvaluationMethod,
    ExperimentStatus,
    ExperimentType,
)


class TestKitRead(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    role_term_id: Optional[str] = None
    created_at: datetime


class TestKitVersionRead(BaseModel):
    id: str
    eval_set_id: str
    version: int
    status: EvalSetVersionStatus
    task_ids: List[str]
    created_at: datetime
    published_at: Optional[datetime] = None
    frozen_at: Optional[datetime] = None


class StarterTestKitsRead(BaseModel):
    test_kits: List[TestKitRead]
    versions: List[TestKitVersionRead]


class TestKitCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=4000)
    task_ids: List[str] = Field(min_length=1, max_length=100)

    class Config:
        extra = "forbid"


class TestKitVersionCreate(BaseModel):
    task_ids: List[str] = Field(min_length=1, max_length=100)

    class Config:
        extra = "forbid"


class ExperimentModelSelection(BaseModel):
    model_id: str
    provider_model_snapshot_id: str


class ExperimentCreate(BaseModel):
    experiment_type: ExperimentType
    hypothesis: Optional[str] = Field(default=None, max_length=4000)
    eval_set_version_id: str
    agent_version_ids: List[str] = Field(min_length=1, max_length=32)
    models: List[ExperimentModelSelection] = Field(default_factory=list, max_length=64)
    repetitions: int = Field(default=1, ge=1, le=100)
    config: dict = Field(default_factory=dict)
    development_id: Optional[str] = None
    concept_id: Optional[str] = None
    learning_item_id: Optional[str] = None
    budget_id: Optional[str] = None

    class Config:
        extra = "forbid"


class ExperimentRead(BaseModel):
    id: str
    user_id: str
    experiment_type: ExperimentType
    status: ExperimentStatus
    hypothesis: Optional[str] = None
    eval_set_version_id: str
    development_id: Optional[str] = None
    concept_id: Optional[str] = None
    concept_version_id: Optional[str] = None
    conclusion: Optional[dict] = None
    learning_item_id: Optional[str] = None
    repetitions: int
    config_snapshot: dict
    estimated_cost: Optional[Decimal] = None
    estimated_currency: str
    cost_estimate_kind: CostEstimateKind
    budget_id: Optional[str] = None
    agent_version_ids: List[str]
    models: List[ExperimentModelSelection]
    frozen_at: Optional[datetime] = None
    created_at: datetime


class ExperimentListRead(BaseModel):
    items: List[ExperimentRead]


class ExperimentEvaluateRequest(BaseModel):
    evaluation_definition_version_id: str
    method: EvaluationMethod = EvaluationMethod.DETERMINISTIC
    evaluator_agent_version_id: Optional[str] = None

    class Config:
        extra = "forbid"


class ExperimentConclusionWrite(BaseModel):
    conclusion_type: ConclusionType
    conclusion_text: Optional[str] = Field(default=None, max_length=4000)

    class Config:
        extra = "forbid"


class ExperimentConceptBind(BaseModel):
    concept_id: str = Field(min_length=1, max_length=36)

    class Config:
        extra = "forbid"

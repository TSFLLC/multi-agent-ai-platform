"""Evaluation Run execution contracts — MA6 Slice 2, extended by Slice 3C,
extended by Slice 3D's read-model/provenance shapes.

Mirrors app.schemas.evaluation_definitions's shape applied to a run's
results instead of a rubric's criteria. No aggregate score/percentage/rank
field exists anywhere here on purpose (MA6 non-negotiable invariant).
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, List, Optional

from pydantic import BaseModel, model_validator

from app.db.enums import EvaluationFinding, EvaluationMethod, EvaluationRunStatus
from app.schemas.common import ORMModel, TimestampedRead


class EvaluationRunCreate(BaseModel):
    """``method`` defaults to DETERMINISTIC -- Slice 2's original request
    shape (no ``method`` field at all) keeps working unchanged. Slice 3B's
    AGENT_EVALUATOR method requires an explicit ``evaluator_agent_version_id``
    -- the operator always picks the evaluator Agent/Version themselves,
    never auto-selected; ``evaluator_model_policy_override`` is optional
    and only meaningful for AGENT_EVALUATOR (same shape as
    ComparisonCandidate.model_policy_override_json)."""

    method: EvaluationMethod = EvaluationMethod.DETERMINISTIC
    subject_artifact_id: str
    evaluation_definition_version_id: str
    evaluator_agent_version_id: Optional[str] = None
    evaluator_model_policy_override: Optional[dict] = None
    budget_id: Optional[str] = None

    @model_validator(mode="after")
    def _require_evaluator_agent_version_for_agent_evaluator(self) -> "EvaluationRunCreate":
        if self.method == EvaluationMethod.AGENT_EVALUATOR and not self.evaluator_agent_version_id:
            raise ValueError("evaluator_agent_version_id is required when method is agent_evaluator.")
        return self


class EvaluationCriterionResultRead(ORMModel):
    id: str
    criterion_key: str
    order_index: int
    finding: EvaluationFinding
    rationale: str
    # List[Any], not List[str]: the deterministic method's evidence is a
    # plain list of artifact-id strings, but AGENT_EVALUATOR's (Slice 3B)
    # is a list of structured {"quote", "criterion_key"} objects -- both
    # are valid, unconstrained JSON already at the model/DB layer.
    evidence_refs: Optional[List[Any]] = None
    # Slice 3D: the immutable registry EvaluationCriterion's own
    # label/description, resolved via evaluation_criterion_id -- never
    # populated by a plain ``from_attributes`` read of this row alone
    # (that FK isn't on EvaluationCriterionResult's own set of plain
    # columns), only by the Slice 3D read-model builders that explicitly
    # join it (app.services.evaluation_execution_service.
    # _criterion_result_details_by_run_id).
    criterion_label: Optional[str] = None
    criterion_description: Optional[str] = None


class EvaluationRunRead(TimestampedRead):
    subject_agent_run_id: str
    subject_artifact_id: str
    subject_artifact_content_hash: str
    evaluation_definition_version_id: str
    method: EvaluationMethod
    status: EvaluationRunStatus
    requested_by_user_id: Optional[str] = None
    # Slice 3B provenance -- always None for method=DETERMINISTIC.
    evaluator_agent_version_id: Optional[str] = None
    evaluator_task_run_id: Optional[str] = None
    evaluator_agent_run_id: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    criterion_results: List[EvaluationCriterionResultRead] = []


# -- Slice 3D: bounded provenance detail for MA6.4 UI ---------------------------


class ModelIdentityRead(BaseModel):
    """Always resolved via an Agent Run's own model_id/provider_id/
    provider_model_snapshot_id FKs (app.services.evaluation_execution_
    service.ModelIdentity) -- never inferred from a model_policy string.
    Every field is ``None`` together when no model was ever resolved for
    that Agent Run (e.g. it hasn't executed yet) -- never fabricated."""

    model_id: Optional[str] = None
    canonical_model_id: Optional[str] = None
    provider_id: Optional[str] = None
    provider_name: Optional[str] = None
    provider_model_snapshot_id: Optional[str] = None


class AgentIdentityRead(BaseModel):
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_version_id: str
    agent_version: Optional[int] = None
    agent_version_role: Optional[str] = None


class ExecutionEvidenceRead(BaseModel):
    """Aggregated from existing ModelCall rows at read time (never copied
    onto EvaluationRun) -- see app.services.evaluation_execution_service.
    _execution_evidence. This whole block is omitted (``None``) on the
    parent when zero ModelCall rows exist yet, never presented as zeros."""

    tokens_in: int
    tokens_out: int
    total_tokens: int
    cost_amount: Decimal
    cost_currency: str
    cost_is_estimated: bool
    latency_ms: int


class EvaluationSubjectRead(BaseModel):
    agent_run_id: str
    agent: AgentIdentityRead
    model: Optional[ModelIdentityRead] = None
    artifact_id: str
    artifact_content_hash: str


class EvaluatorRead(BaseModel):
    """``None`` on the parent for method=DETERMINISTIC -- never a
    fabricated/empty-but-present evaluator block for a method that has no
    evaluator at all."""

    agent: AgentIdentityRead
    agent_run_id: str
    model: Optional[ModelIdentityRead] = None
    execution_evidence: Optional[ExecutionEvidenceRead] = None


class EvaluationDefinitionIdentityRead(BaseModel):
    evaluation_definition_id: str
    evaluation_definition_name: str
    evaluation_definition_version_id: str
    evaluation_definition_version: int


class EvaluationRunDetailRead(EvaluationRunRead):
    """GET /evaluation-runs/{id}'s full response shape (Section: MA6 Slice
    3D) -- a strict superset of EvaluationRunRead's own flat fields (every
    existing consumer of the plain shape keeps working unchanged), plus
    the subject/evaluator/definition provenance blocks MA6.4 needs. No
    aggregate score/percentage/rank/winner field exists here, same
    invariant as the rest of this module."""

    subject: EvaluationSubjectRead
    evaluation_definition: EvaluationDefinitionIdentityRead
    evaluator: Optional[EvaluatorRead] = None

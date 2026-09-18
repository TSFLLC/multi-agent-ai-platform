"""Evaluation Run execution contracts — MA6 Slice 2, extended by Slice 3C.

Mirrors app.schemas.evaluation_definitions's shape applied to a run's
results instead of a rubric's criteria. No aggregate score/percentage/rank
field exists anywhere here on purpose (MA6 non-negotiable invariant).
"""

from datetime import datetime
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

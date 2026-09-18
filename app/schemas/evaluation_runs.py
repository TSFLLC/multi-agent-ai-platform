"""Evaluation Run execution contracts — MA6 Slice 2.

Mirrors app.schemas.evaluation_definitions's shape applied to a run's
results instead of a rubric's criteria. No aggregate score/percentage/rank
field exists anywhere here on purpose (MA6 non-negotiable invariant).
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from app.db.enums import EvaluationFinding, EvaluationMethod, EvaluationRunStatus
from app.schemas.common import ORMModel, TimestampedRead


class EvaluationRunCreate(BaseModel):
    subject_artifact_id: str
    evaluation_definition_version_id: str


class EvaluationCriterionResultRead(ORMModel):
    id: str
    criterion_key: str
    order_index: int
    finding: EvaluationFinding
    rationale: str
    evidence_refs: Optional[List[str]] = None


class EvaluationRunRead(TimestampedRead):
    subject_agent_run_id: str
    subject_artifact_id: str
    subject_artifact_content_hash: str
    evaluation_definition_version_id: str
    method: EvaluationMethod
    status: EvaluationRunStatus
    requested_by_user_id: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    criterion_results: List[EvaluationCriterionResultRead] = []

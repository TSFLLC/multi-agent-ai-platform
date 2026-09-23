"""Learning Evidence Service — AIL.1A, docs/ail-learning-spec-v1.md Sec 18.4.

Append-only by construction: this module exposes exactly one write
method, ``record_evidence``, and it always ``INSERT``s a new row — there
is no ``update``/``delete`` method here at all. The only way any
``learning_evidence`` row is ever removed is
``app.services.learner_profile_service.LearnerProfileService.
delete_learner_data`` (the full-user-deletion path, spec Sec 30).
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import EvidenceRefType, EvidenceType, GradingMode, QuestionOrigin
from app.models.learner import LearningEvidence


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LearningEvidenceService:
    def __init__(self, db: Session):
        self.db = db

    def record_evidence(
        self,
        *,
        user_id: str,
        concept_id: str,
        concept_version_id: str,
        evidence_type: EvidenceType,
        grader: GradingMode,
        learning_item_id: Optional[str] = None,
        score: Optional[dict] = None,
        passed: Optional[bool] = None,
        grader_confidence: Optional[float] = None,
        question_origin: Optional[QuestionOrigin] = None,
        on_demo_data: bool = False,
        ref_type: EvidenceRefType = EvidenceRefType.NONE,
        ref_id: Optional[str] = None,
    ) -> LearningEvidence:
        evidence = LearningEvidence(
            user_id=user_id,
            concept_id=concept_id,
            concept_version_id=concept_version_id,
            evidence_type=evidence_type,
            learning_item_id=learning_item_id,
            score=score,
            passed=passed,
            grader=grader,
            grader_confidence=grader_confidence,
            question_origin=question_origin,
            on_demo_data=on_demo_data,
            ref_type=ref_type,
            ref_id=ref_id,
            created_at=_utcnow(),
        )
        self.db.add(evidence)
        self.db.commit()
        self.db.refresh(evidence)
        return evidence

    def list_evidence(self, user_id: str, *, concept_id: Optional[str] = None) -> List[LearningEvidence]:
        stmt = select(LearningEvidence).where(LearningEvidence.user_id == user_id)
        if concept_id is not None:
            stmt = stmt.where(LearningEvidence.concept_id == concept_id)
        stmt = stmt.order_by(LearningEvidence.created_at)
        return list(self.db.execute(stmt).scalars().all())

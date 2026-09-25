"""Learning Evidence Service — AIL.1A, docs/ail-learning-spec-v1.md Sec 18.4.

Append-only by construction: this module exposes exactly one row-creating
method, ``record_evidence``, and it always ``INSERT``s a new row — there
is no ``update``/``delete`` method here at all. The only other write is
``supersede`` (AIL.5C), which sets a single pointer and edits nothing else. The only way any
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
        assistance_level=None,
        execution_verification=None,
        milestone_attempt_id: Optional[str] = None,
        commit: bool = True,
    ) -> LearningEvidence:
        """Append one evidence row — the single canonical write path.

        ``commit=False`` (AIL.4B) only flushes, so a caller that must make the
        evidence and another row atomic (a review attempt and its resulting
        evidence) can commit or roll both back together. Nothing else about
        the row, the validation, or its append-only nature differs.
        """
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
            assistance_level=assistance_level,
            execution_verification=execution_verification,
            milestone_attempt_id=milestone_attempt_id,
            created_at=_utcnow(),
        )
        self.db.add(evidence)
        if commit:
            self.db.commit()
            self.db.refresh(evidence)
        else:
            self.db.flush()
        return evidence

    def supersede(self, old_id: str, new_id: str, *, commit: bool = True) -> LearningEvidence:
        """AIL.5C: the ONE narrowly scoped pointer setter for the documented
        dispute convention ("append a new row and point the old row at it",
        docs/ail-learning-spec-v1.md Sec 18.4). It only ever sets
        ``superseded_by_id``, never edits any other field, refuses to
        re-point an already superseded row, and requires the same learner and
        Concept on both rows. Only the assessment review service calls it
        (an AST test enforces that)."""
        old = self.db.get(LearningEvidence, old_id)
        new = self.db.get(LearningEvidence, new_id)
        if old is None or new is None:
            raise ValueError("Both evidence rows must exist.")
        if old.user_id != new.user_id or old.concept_id != new.concept_id or old.id == new.id:
            raise ValueError("A row can only be superseded by another row of the same learner and Concept.")
        if old.superseded_by_id is not None:
            raise ValueError("This evidence row has already been superseded.")
        old.superseded_by_id = new.id
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return old

    def list_evidence(self, user_id: str, *, concept_id: Optional[str] = None) -> List[LearningEvidence]:
        stmt = select(LearningEvidence).where(LearningEvidence.user_id == user_id)
        if concept_id is not None:
            stmt = stmt.where(LearningEvidence.concept_id == concept_id)
        stmt = stmt.order_by(LearningEvidence.created_at)
        return list(self.db.execute(stmt).scalars().all())

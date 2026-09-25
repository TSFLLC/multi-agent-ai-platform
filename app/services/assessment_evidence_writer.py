"""AIL.5C evidence writer — the ONLY way an assessment reaches Learner State.

    ASSESSMENT RESULT (final, passed)
      -> qualification (demonstration effect)
      -> APPEND ``learning_evidence`` (idempotent)
      -> LearnerStateService recomputes on read

This module never sets UNDERSTOOD / PRACTICED / DEMONSTRATED and never touches
a ladder: it appends evidence, nothing more. ``LearnerStateService`` is the only
place a rung is derived, from the evidence plus the pinned Concept Version's
requirement set, through the one independence policy.

Rules:
* only a FINAL result with outcome PASSED writes anything — failed,
  provisional, human-review-required and unable-to-assess results write no
  mastery evidence;
* ``formative_only`` (H5 source work, or a declared AI assistant) writes
  nothing: it is feedback, not evidence;
* ``counts_toward_practiced_only`` writes a row whose recorded assistance /
  verification keeps it below the DEMONSTRATED floor;
* idempotent: one row per ``(learner, assessment result, concept)``, enforced
  by the database (partial unique index) and by find-or-insert here, so a
  replay or retry creates nothing new.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.enums import (
    AssessmentKind,
    AssessmentOutcome,
    AssistanceLevel,
    DemonstrationEffect,
    EvidenceRefType,
    EvidenceType,
    ExecutionVerification,
    GraderConfidence,
    GradingMode,
    QuestionOrigin,
)
from app.models.assessment import (
    AssessmentAttempt,
    AssessmentDefinition,
    AssessmentDefinitionConcept,
    AssessmentResult,
)
from app.models.learner import LearningEvidence
from app.services.independence_policy import ASSESSMENT_MODE_ASSISTANCE, EXECUTION_EVIDENCE_TYPES
from app.services.learning_evidence_service import LearningEvidenceService

# Categorical confidence -> the fixed numbers stored in the existing
# ``learning_evidence.grader_confidence`` column. The UI shows the label only.
CONFIDENCE_VALUE = {
    GraderConfidence.HIGH.value: 0.9,
    GraderConfidence.MEDIUM.value: 0.6,
    GraderConfidence.LOW.value: 0.3,
}


def evidence_assistance(independence: Dict[str, Any]) -> Optional[AssistanceLevel]:
    """Fresh work in Assessment Mode is H0 by construction (the Mentor was
    locked); source-work assessments inherit the worst recorded level."""
    if independence.get("challenge_issued"):
        return ASSESSMENT_MODE_ASSISTANCE
    top = independence.get("source_max_assistance")
    return AssistanceLevel(top) if top else None


class AssessmentEvidenceWriter:
    def __init__(self, db: Session):
        self.db = db
        self._evidence = LearningEvidenceService(db)

    def existing(self, user_id: str, result_id: str, concept_id: str) -> Optional[LearningEvidence]:
        return (
            self.db.execute(
                select(LearningEvidence).where(
                    LearningEvidence.user_id == user_id,
                    LearningEvidence.ref_type == EvidenceRefType.ASSESSMENT_RESULT,
                    LearningEvidence.ref_id == result_id,
                    LearningEvidence.concept_id == concept_id,
                )
            )
            .scalars()
            .first()
        )

    def write(
        self,
        *,
        attempt: AssessmentAttempt,
        definition: AssessmentDefinition,
        links: List[AssessmentDefinitionConcept],
        result: AssessmentResult,
        grader: Optional[GradingMode] = None,
    ) -> List[LearningEvidence]:
        """Append (or find) the evidence rows for a FINAL passing result.
        Returns the rows; writes nothing for any other outcome or effect."""
        if result.outcome != AssessmentOutcome.PASSED:
            return []
        if result.demonstration_effect not in (
            DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED,
            DemonstrationEffect.COUNTS_TOWARD_PRACTICED_ONLY,
        ):
            return []

        facts = result.facts or {}
        independence = facts.get("independence") or {}
        verification = ExecutionVerification(
            facts.get("execution_verification") or ExecutionVerification.NOT_APPLICABLE.value
        )
        evidence_type = definition.produces_evidence_type
        if evidence_type not in EXECUTION_EVIDENCE_TYPES and evidence_type != EvidenceType.LAB:
            verification = ExecutionVerification.NOT_APPLICABLE

        by_key = {c["key"]: c for c in result.criteria}
        rows: List[LearningEvidence] = []
        for link in links:
            deciding = [
                by_key[k] for k in link.criterion_keys if k in by_key and by_key[k].get("required", True)
            ]
            if not deciding:
                continue
            judged = [c for c in deciding if c.get("method") == "grader"]
            row_grader = grader or (GradingMode.AI_RUBRIC if judged else GradingMode.DETERMINISTIC)
            confidence = None
            if judged and row_grader == GradingMode.AI_RUBRIC:
                lowest = min((c["confidence"] for c in judged), key=lambda v: CONFIDENCE_VALUE[v])
                confidence = CONFIDENCE_VALUE[lowest]
            rows.append(
                self._find_or_insert(
                    user_id=attempt.user_id,
                    link=link,
                    evidence_type=evidence_type,
                    grader=row_grader,
                    score={
                        "criteria_met": sum(1 for c in deciding if c["finding"] in ("met", "not_applicable")),
                        "criteria_total": len(deciding),
                    },
                    assistance=evidence_assistance(independence),
                    verification=verification,
                    confidence=confidence,
                    result_id=result.id,
                    reviewed_items=definition.assessment_kind == AssessmentKind.KNOWLEDGE_CHECK,
                )
            )
        return rows

    def _find_or_insert(
        self,
        *,
        user_id: str,
        link: AssessmentDefinitionConcept,
        evidence_type: EvidenceType,
        grader: GradingMode,
        score: Dict[str, int],
        assistance: Optional[AssistanceLevel],
        verification: ExecutionVerification,
        confidence: Optional[float],
        result_id: str,
        reviewed_items: bool,
    ) -> LearningEvidence:
        found = self.existing(user_id, result_id, link.concept_id)
        if found is not None:
            return found
        try:
            with self.db.begin_nested():
                return self._evidence.record_evidence(
                    user_id=user_id,
                    concept_id=link.concept_id,
                    concept_version_id=link.concept_version_id,  # pinned, never "latest"
                    evidence_type=evidence_type,
                    grader=grader,
                    score=score,
                    passed=True,
                    grader_confidence=confidence,
                    question_origin=QuestionOrigin.REVIEWED if reviewed_items else None,
                    on_demo_data=False,
                    ref_type=EvidenceRefType.ASSESSMENT_RESULT,
                    ref_id=result_id,
                    assistance_level=assistance,
                    execution_verification=verification,
                    commit=False,
                )
        except IntegrityError:
            # A concurrent writer won the race; the database index made us a replay.
            found = self.existing(user_id, result_id, link.concept_id)
            if found is None:
                raise
            return found

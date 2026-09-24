"""AIL.4B review attempts — start and complete a retention review.

The review is one deterministic check (spec Sec 22.2 "1 record-grounded
question / 1 scenario"). This service owns the lifecycle
STARTED -> PASSED | FAILED and nothing else: which Concepts are due is derived
by ``ReviewAssessor`` (through ``LearnerStateService``), and ``LearnerStateService``
stays the only authority for the ladder.

Successful-review evidence contract — a PASSED attempt is never enough:

1. The attempt's item must satisfy the review evidence-quality contract
   (``qualifies_as_review_item``): it belongs to the attempt's Concept, is
   ``reviewed``, is a deterministic check/scenario item, and carries a valid
   choice spec with an answer key. Otherwise the completion is refused and the
   attempt stays STARTED — nothing is graded, nothing is recorded.
2. The submission must be well formed (indices in range, no duplicates, the
   right number for a single-choice item). Otherwise it is refused (422) and
   the attempt stays STARTED — an incomplete or invalid attempt never fails or
   passes a learner.
3. It is graded deterministically, server side, from the item's answer key.
4. Only a correct answer appends evidence, through the canonical
   ``LearningEvidenceService.record_evidence`` (knowledge_check, deterministic,
   question_origin=reviewed, not demo data), recorded against the ConceptVersion
   frozen when the attempt started. The attempt and its evidence are committed
   in ONE transaction; the database refuses a PASSED row without evidence.
5. A wrong answer marks the attempt FAILED and appends nothing. Failure never
   deletes or downgrades anything and never touches a Learning Plan.

Completion is idempotent and race-safe: the STARTED -> terminal transition is a
compare-and-set, and the loser of a concurrent completion rolls back its
evidence and reports the winner's result.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.db.enums import (
    EvidenceRefType,
    EvidenceType,
    GradingMode,
    LearningItemType,
    QuestionOrigin,
    ReviewAttemptStatus,
)
from app.errors import AppError, ConflictError, NotFoundError
from app.models.concepts import Concept, LearningItem
from app.models.learner import LearningEvidence
from app.models.learning_review import ReviewAttempt
from app.services.concept_graph_service import ConceptGraphService
from app.services.learner_state_service import DEMONSTRATED, LearnerConceptState, LearnerStateService
from app.services.learning_evidence_service import LearningEvidenceService
from app.services.review_retention_service import _aware

_REVIEW_ITEM_TYPES = (LearningItemType.CHECK_QUESTION, LearningItemType.SCENARIO)
_MIN = datetime.min.replace(tzinfo=timezone.utc)


class ReviewNotAvailableError(ConflictError):
    code = "review_not_available"


class ReviewItemNotQualifyingError(ConflictError):
    code = "review_item_not_qualifying"


class ReviewSubmissionInvalidError(AppError):
    status_code = 422
    code = "review_submission_invalid"


_UNAVAILABLE_MESSAGES = {
    "NOT_FOUND": "That Concept does not exist.",
    "NOT_DEMONSTRATED": "Only a Concept you have demonstrated can be reviewed.",
    "NOT_ELIGIBLE": "Reviews apply to core Concepts and Concepts in your active plan.",
    "NOT_DUE": "This Concept is not due for review.",
    "COOLDOWN": "A retry is available after a short wait.",
    "NO_REVIEW_ITEM": "No reviewed check is available for this Concept yet.",
    "NO_VERSION": "This Concept has no published version to review against.",
}


# -- the review evidence-quality contract -----------------------------------------


def choice_spec(item: LearningItem) -> Optional[Dict[str, Any]]:
    """A validated choice spec, or None. Expected shape (a payload on
    ``learning_items.spec``): ``{"kind": "choice", "options": [str, ...],
    "answer_key": [int, ...], "multiple": bool}``."""
    spec = item.spec
    if not isinstance(spec, dict) or spec.get("kind") != "choice":
        return None
    options, key = spec.get("options"), spec.get("answer_key")
    if not isinstance(options, list) or len(options) < 2 or not all(isinstance(o, str) and o.strip() for o in options):
        return None
    if not isinstance(key, list) or not key or len(set(key)) != len(key):
        return None
    if not all(isinstance(k, int) and not isinstance(k, bool) and 0 <= k < len(options) for k in key):
        return None
    multiple = bool(spec.get("multiple", False))
    if not multiple and len(key) != 1:
        return None
    return {"options": list(options), "answer_key": sorted(key), "multiple": multiple}


def qualifies_as_review_item(item: Optional[LearningItem], concept_id: str) -> Optional[str]:
    """None when the item may back a review; otherwise a reason code."""
    if item is None or item.concept_id != concept_id:
        return "NOT_A_CONCEPT_ITEM"
    if not item.reviewed:
        return "NOT_REVIEWED"
    if item.item_type not in _REVIEW_ITEM_TYPES:
        return "WRONG_ITEM_TYPE"
    if item.grading_mode != GradingMode.DETERMINISTIC:
        return "NOT_DETERMINISTIC"
    if choice_spec(item) is None:
        return "INVALID_SPEC"
    return None


# -- results -------------------------------------------------------------------------


@dataclass
class ReviewAvailability:
    action: str  # "start" | "continue" | "unavailable"
    reason: Optional[str] = None
    available_after: Optional[datetime] = None
    learning_item_id: Optional[str] = None
    attempt_id: Optional[str] = None


@dataclass
class StartResult:
    attempt: ReviewAttempt
    item: Optional[LearningItem]
    created: bool


@dataclass
class CompletionResult:
    attempt: ReviewAttempt
    passed: bool
    evidence: Optional[LearningEvidence]
    replay: bool
    state: LearnerConceptState


class ReviewAttemptService:
    def __init__(self, db: Session, *, now: Optional[datetime] = None):
        self.db = db
        self._now = _aware(now)
        self._concepts = ConceptGraphService(db)
        self._learner_state = LearnerStateService(db)

    def _clock(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    # -- reads -----------------------------------------------------------------------

    def get(self, user_id: str, attempt_id: str) -> ReviewAttempt:
        attempt = self.db.get(ReviewAttempt, attempt_id)
        if attempt is None or attempt.user_id != user_id:
            raise NotFoundError("Review attempt not found.")
        return attempt

    def item_for(self, attempt: ReviewAttempt) -> Optional[LearningItem]:
        return self.db.get(LearningItem, attempt.learning_item_id) if attempt.learning_item_id else None

    def availability(
        self, user_id: str, concept_id: str, *, state: Optional[LearnerConceptState] = None
    ) -> ReviewAvailability:
        """What the learner can do about this Concept right now. Read-only:
        the same decision ``start`` acts on, so a card never offers what the
        API would refuse."""
        if self.db.get(Concept, concept_id) is None:
            return ReviewAvailability("unavailable", "NOT_FOUND")
        state = state or self._learner_state.state(user_id, concept_id, now=self._clock())
        review = state.review
        if review is None:
            return ReviewAvailability("unavailable", "NOT_FOUND")
        if review.active_attempt is not None:
            return ReviewAvailability(
                "continue", attempt_id=review.active_attempt.id, learning_item_id=review.active_attempt.learning_item_id
            )
        if state.ladder != DEMONSTRATED:
            return ReviewAvailability("unavailable", "NOT_DEMONSTRATED")
        if not review.eligible:
            return ReviewAvailability("unavailable", "NOT_ELIGIBLE")
        if not (review.due or review.failed):
            return ReviewAvailability("unavailable", "NOT_DUE")
        if review.cooldown_until is not None and self._clock() < review.cooldown_until:
            return ReviewAvailability("unavailable", "COOLDOWN", available_after=review.cooldown_until)
        if self._concepts.get_current_version(concept_id) is None:
            return ReviewAvailability("unavailable", "NO_VERSION")
        item = self._select_item(user_id, concept_id)
        if item is None:
            return ReviewAvailability("unavailable", "NO_REVIEW_ITEM")
        return ReviewAvailability("start", learning_item_id=item.id)

    def _select_item(self, user_id: str, concept_id: str) -> Optional[LearningItem]:
        """Deterministic, not random: qualifying items only; never the item of
        the latest failed review when another exists; least recently used
        first, then by id."""
        items = [
            i
            for i in self.db.execute(
                select(LearningItem).where(LearningItem.concept_id == concept_id).order_by(LearningItem.id)
            ).scalars()
            if qualifies_as_review_item(i, concept_id) is None
        ]
        if not items:
            return None
        attempts = list(
            self.db.execute(
                select(ReviewAttempt)
                .where(ReviewAttempt.user_id == user_id, ReviewAttempt.concept_id == concept_id)
                .order_by(ReviewAttempt.started_at, ReviewAttempt.id)
            ).scalars()
        )
        last_used: Dict[str, datetime] = {}
        for attempt in attempts:
            if attempt.learning_item_id:
                last_used[attempt.learning_item_id] = _aware(attempt.started_at)
        latest_failed = next(
            (a for a in reversed(attempts) if a.status == ReviewAttemptStatus.FAILED and a.learning_item_id), None
        )
        candidates = items
        if latest_failed is not None and len(items) > 1:
            candidates = [i for i in items if i.id != latest_failed.learning_item_id] or items
        return min(candidates, key=lambda i: (last_used.get(i.id, _MIN), i.id))

    # -- start -------------------------------------------------------------------------

    def start(self, user_id: str, concept_id: str) -> StartResult:
        availability = self.availability(user_id, concept_id)
        if availability.action == "continue":
            attempt = self.get(user_id, availability.attempt_id)
            return StartResult(attempt, self.item_for(attempt), created=False)
        if availability.action != "start":
            reason = availability.reason or "NOT_DUE"
            if reason == "NOT_FOUND":
                raise NotFoundError(_UNAVAILABLE_MESSAGES[reason])
            raise ReviewNotAvailableError(
                _UNAVAILABLE_MESSAGES.get(reason, "A review is not available."),
                detail={
                    "reason": reason,
                    "available_after": availability.available_after.isoformat() if availability.available_after else None,
                },
            )
        version = self._concepts.get_current_version(concept_id)
        attempt = ReviewAttempt(
            user_id=user_id,
            concept_id=concept_id,
            concept_version_id=version.id,
            learning_item_id=availability.learning_item_id,
            status=ReviewAttemptStatus.STARTED,
            started_at=self._clock(),
        )
        self.db.add(attempt)
        try:
            self.db.commit()
        except IntegrityError:
            # Lost a start race: the partial unique index allows one STARTED
            # attempt per learner and Concept. Return the winner's.
            self.db.rollback()
            existing = self._active_attempt(user_id, concept_id)
            if existing is None:
                raise
            return StartResult(existing, self.item_for(existing), created=False)
        self.db.refresh(attempt)
        return StartResult(attempt, self.item_for(attempt), created=True)

    def _active_attempt(self, user_id: str, concept_id: str) -> Optional[ReviewAttempt]:
        return self.db.execute(
            select(ReviewAttempt).where(
                ReviewAttempt.user_id == user_id,
                ReviewAttempt.concept_id == concept_id,
                ReviewAttempt.status == ReviewAttemptStatus.STARTED,
            )
        ).scalar_one_or_none()

    # -- complete ----------------------------------------------------------------------

    def complete(self, user_id: str, attempt_id: str, selected: List[int]) -> CompletionResult:
        attempt = self.get(user_id, attempt_id)
        if attempt.status != ReviewAttemptStatus.STARTED:
            return self._replay(user_id, attempt)

        item = self.item_for(attempt)
        reason = qualifies_as_review_item(item, attempt.concept_id)
        if reason is not None:
            raise ReviewItemNotQualifyingError(
                "This review's check does not meet the evidence-quality requirements, so it cannot be completed.",
                detail={"reason": reason},
            )
        spec = choice_spec(item)
        selection = self._validated_selection(selected, spec)
        passed = set(selection) == set(spec["answer_key"])

        completed_at = self._clock()
        try:
            evidence_id: Optional[str] = None
            if passed:
                evidence = LearningEvidenceService(self.db).record_evidence(
                    user_id=user_id,
                    concept_id=attempt.concept_id,
                    concept_version_id=attempt.concept_version_id,
                    evidence_type=EvidenceType.KNOWLEDGE_CHECK,
                    grader=GradingMode.DETERMINISTIC,
                    learning_item_id=item.id,
                    score={"raw": 1, "max": 1, "pct": 100},
                    passed=True,
                    question_origin=QuestionOrigin.REVIEWED,
                    on_demo_data=False,
                    ref_type=EvidenceRefType.NONE,
                    commit=False,
                )
                evidence_id = evidence.id
            result = self.db.execute(
                update(ReviewAttempt)
                .where(ReviewAttempt.id == attempt.id, ReviewAttempt.status == ReviewAttemptStatus.STARTED)
                .values(
                    status=ReviewAttemptStatus.PASSED if passed else ReviewAttemptStatus.FAILED,
                    resulting_learning_evidence_id=evidence_id,
                    completed_at=completed_at,
                )
            )
            if result.rowcount != 1:
                self.db.rollback()  # lost the race: the winner's evidence stands, ours is discarded
                return self._replay(user_id, self.get(user_id, attempt_id))
            self.db.commit()
        except (IntegrityError, OperationalError):
            self.db.rollback()
            current = self.get(user_id, attempt_id)
            if current.status != ReviewAttemptStatus.STARTED:
                return self._replay(user_id, current)
            raise
        self.db.expire_all()
        return self._result(user_id, self.get(user_id, attempt_id), replay=False)

    @staticmethod
    def _validated_selection(selected: Any, spec: Dict[str, Any]) -> List[int]:
        if not isinstance(selected, list) or not selected:
            raise ReviewSubmissionInvalidError("Choose an answer before submitting.")
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in selected):
            raise ReviewSubmissionInvalidError("Answers must be the numbers of the options you chose.")
        if len(set(selected)) != len(selected):
            raise ReviewSubmissionInvalidError("Each option can be chosen only once.")
        if any(i < 0 or i >= len(spec["options"]) for i in selected):
            raise ReviewSubmissionInvalidError("One of the chosen options does not exist.")
        if not spec["multiple"] and len(selected) != 1:
            raise ReviewSubmissionInvalidError("This question takes exactly one answer.")
        return list(selected)

    def _replay(self, user_id: str, attempt: ReviewAttempt) -> CompletionResult:
        return self._result(user_id, attempt, replay=True)

    def _result(self, user_id: str, attempt: ReviewAttempt, *, replay: bool) -> CompletionResult:
        evidence = (
            self.db.get(LearningEvidence, attempt.resulting_learning_evidence_id)
            if attempt.resulting_learning_evidence_id
            else None
        )
        state = self._learner_state.state(user_id, attempt.concept_id, now=self._clock())
        return CompletionResult(
            attempt=attempt,
            passed=attempt.status == ReviewAttemptStatus.PASSED,
            evidence=evidence,
            replay=replay,
            state=state,
        )

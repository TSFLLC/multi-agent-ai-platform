"""AIL.4B review attempts — owner-authorized, explicit, user-initiated.

Nothing here runs on its own: a review starts only when the authenticated
user asks, and completes only with their answer. The authenticated user is the
only identity used; every route answers 404 for another user's attempt.
"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.models.identity import User
from app.models.learning_review import ReviewAttempt
from app.schemas.learning_review import (
    ReviewAttemptRead,
    ReviewCompleteRead,
    ReviewCompleteRequest,
    ReviewItemRead,
    ReviewLearnerStateRead,
    ReviewStartRead,
    ReviewStartRequest,
)
from app.services.review_attempt_service import ReviewAttemptService, choice_spec

router = APIRouter(prefix="/learning-reviews", tags=["learning-reviews"])


def _attempt_read(attempt: ReviewAttempt) -> ReviewAttemptRead:
    return ReviewAttemptRead(
        id=attempt.id,
        concept_id=attempt.concept_id,
        concept_version_id=attempt.concept_version_id,
        learning_item_id=attempt.learning_item_id,
        status=attempt.status.value,
        resulting_learning_evidence_id=attempt.resulting_learning_evidence_id,
        started_at=attempt.started_at,
        completed_at=attempt.completed_at,
    )


def _item_read(item):
    spec = choice_spec(item) if item is not None else None
    if item is None or spec is None:
        return None
    return ReviewItemRead(
        id=item.id,
        title=item.title,
        body_md=item.body_md,
        item_type=item.item_type.value,
        options=spec["options"],
        multiple=spec["multiple"],
    )  # the answer key is never returned


@router.post("", response_model=ReviewStartRead, status_code=201)
def start_review(
    body: ReviewStartRequest,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = ReviewAttemptService(db).start(user.id, body.concept_id)
    if not result.created:
        response.status_code = 200  # idempotent: the in-progress attempt is returned
    return ReviewStartRead(attempt=_attempt_read(result.attempt), item=_item_read(result.item), created=result.created)


@router.get("/{attempt_id}", response_model=ReviewStartRead)
def get_review(attempt_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = ReviewAttemptService(db)
    attempt = service.get(user.id, attempt_id)
    item = service.item_for(attempt) if attempt.status.value == "started" else None
    return ReviewStartRead(attempt=_attempt_read(attempt), item=_item_read(item), created=False)


@router.post("/{attempt_id}/complete", response_model=ReviewCompleteRead)
def complete_review(
    attempt_id: str,
    body: ReviewCompleteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = ReviewAttemptService(db).complete(user.id, attempt_id, body.selected)
    if result.passed:
        message = (
            "Correct. This review was recorded as learning evidence, and your review clock has been reset."
        )
    else:
        message = (
            "Not quite. Your record is unchanged: the Concept stays Demonstrated. "
            "You can review it again after a short wait."
        )
    return ReviewCompleteRead(
        attempt=_attempt_read(result.attempt),
        passed=result.passed,
        evidence_id=result.evidence.id if result.evidence is not None else None,
        replay=result.replay,
        learner_state=ReviewLearnerStateRead(ladder=result.state.ladder, overlays=sorted(result.state.overlays)),
        message=message,
    )

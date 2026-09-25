"""AIL.5C Assessment Mode: the Mentor / Professor lock.

Assessment Mode is exactly this: while a learner has a live DRAFT attempt, the
Mentor and Professor are unavailable for the concepts / project being
assessed. It is not surveillance — no webcam, screen, keystroke, focus or
clipboard capture exists anywhere; the only clock is the server-side
``expires_at``. Mentor history is never touched, and "Leave assessment"
(abandon) releases the lock at once.

Kept free of any grading code so importing it from Mentor / Professor
execution can never give them a path to grading (an import-graph test checks).
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.enums import AssessmentAttemptStatus
from app.errors import ConflictError
from app.models.assessment import AssessmentAttempt, AssessmentDefinition, AssessmentDefinitionConcept

DRAFT_RETENTION_DAYS = 30


class AssessmentModeActive(ConflictError):
    code = "assessment_mode_active"


def _aware(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


class AssessmentModeGuard:
    def __init__(self, db: Session, *, now: Optional[datetime] = None):
        self.db = db
        self._now = now

    def _clock(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def expire_stale(self, user_id: str) -> int:
        """Expired DRAFT attempts are abandoned (releasing the lock). Their
        rows and drafts are kept; only the status changes."""
        now = self._clock()
        expired = 0
        rows = (
            self.db.execute(
                select(AssessmentAttempt).where(
                    AssessmentAttempt.user_id == user_id,
                    AssessmentAttempt.status == AssessmentAttemptStatus.DRAFT,
                )
            )
            .scalars()
            .all()
        )
        for attempt in rows:
            if attempt.expires_at is not None and _aware(attempt.expires_at) <= now:
                attempt.status = AssessmentAttemptStatus.ABANDONED
                expired += 1
        if expired:
            self.db.commit()
        self.purge_stale_drafts(user_id)
        return expired

    def purge_stale_drafts(self, user_id: str) -> int:
        """Unsubmitted drafts of abandoned attempts are cleared after 30 days.
        The attempt row itself is kept (as ABANDONED) — only the learner's
        unsent words go."""
        cutoff = self._clock() - timedelta(days=DRAFT_RETENTION_DAYS)
        stale = (
            self.db.execute(
                select(AssessmentAttempt).where(
                    AssessmentAttempt.user_id == user_id,
                    AssessmentAttempt.status == AssessmentAttemptStatus.ABANDONED,
                    AssessmentAttempt.started_at < cutoff,
                )
            )
            .scalars()
            .all()
        )
        purged = 0
        for attempt in stale:
            if attempt.draft:
                self.db.execute(
                    update(AssessmentAttempt)
                    .where(AssessmentAttempt.id == attempt.id)
                    .values(draft={})
                    .execution_options(synchronize_session=False)
                )
                purged += 1
        if purged:
            self.db.commit()
        return purged

    def active_locks(self, user_id: str) -> List[Dict]:
        self.expire_stale(user_id)
        rows = self.db.execute(
            select(AssessmentAttempt, AssessmentDefinition)
            .join(AssessmentDefinition, AssessmentDefinition.id == AssessmentAttempt.definition_id)
            .where(
                AssessmentAttempt.user_id == user_id,
                AssessmentAttempt.status == AssessmentAttemptStatus.DRAFT,
            )
        ).all()
        locks = []
        for attempt, definition in rows:
            concept_ids = [
                c
                for c in self.db.execute(
                    select(AssessmentDefinitionConcept.concept_id).where(
                        AssessmentDefinitionConcept.definition_id == definition.id
                    )
                ).scalars()
            ]
            locks.append(
                {
                    "attempt_id": attempt.id,
                    "definition_key": definition.definition_key,
                    "title": definition.title,
                    "concept_ids": concept_ids,
                    "project_attempt_id": attempt.project_attempt_id,
                    "expires_at": attempt.expires_at,
                }
            )
        return locks

    def _raise(self, lock: Dict, what: str) -> None:
        raise AssessmentModeActive(
            f"{what} is paused while you are in Assessment Mode for '{lock['title']}'. "
            "Leave the assessment to use it again.",
            detail={"attempt_id": lock["attempt_id"], "definition_key": lock["definition_key"]},
        )

    def assert_mentor_available(
        self, user_id: str, *, project_attempt_id: Optional[str] = None, concept_ids: Iterable[str] = ()
    ) -> None:
        wanted = set(concept_ids)
        for lock in self.active_locks(user_id):
            if (project_attempt_id and lock["project_attempt_id"] == project_attempt_id) or wanted & set(
                lock["concept_ids"]
            ):
                self._raise(lock, "The Project Mentor")

    def assert_professor_available(
        self, user_id: str, *, concept_ids: Iterable[str] = (), untargeted: bool = False
    ) -> None:
        """An untargeted question cannot be matched to a concept, so it is held
        too — otherwise "just ask the Professor" would sidestep the lock."""
        wanted = set(concept_ids)
        for lock in self.active_locks(user_id):
            if untargeted or wanted & set(lock["concept_ids"]):
                self._raise(lock, "The AI Professor")

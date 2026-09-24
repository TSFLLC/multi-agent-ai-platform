"""AIL.4B weekly review-prompt quota — "at most two review prompts per week"
(docs/ail-learning-spec-v1.md Sec 22.2; AIL.4 acceptance criterion 2).

Two different questions, kept apart on purpose:

* Permission — may the learner review this Concept? Decided by
  ``ReviewAttemptService`` from eligibility, demonstrated history, a valid
  reviewed item, the single in-progress attempt and the failed-review cooldown.
  The weekly quota NEVER affects it: voluntary review is always possible.
* Prompting — is this review *proactively surfaced* to the learner on Today?
  Decided here. Only prompts that have been *delivered* appear on Today.

One prompt delivery — exact definition
--------------------------------------
A delivery is one persisted ``review_prompt_deliveries`` row: the first-time
surfacing of a review prompt for ONE Concept to ONE user in ONE UTC calendar
week (Monday 00:00 UTC to the next Monday). Its identity is
(user, concept, week). Recomputing or reading Today creates nothing, so the
same logical prompt is never counted twice and a refresh never consumes quota.
A delivered prompt keeps its slot for the rest of the week even if the learner
reviews the Concept in the meantime (the prompt was delivered).

How two per week is enforced
----------------------------
``slot`` is 1 or 2 and UNIQUE per (user, week) in the database, so a third
delivery cannot exist even under concurrent allocation. Allocation fills the
lowest free slots deterministically with the highest-ordered undelivered
candidates; extra eligible Concepts stay known (``not_prompted``) but are not
newly prompted until a later week (or a freed week) — when still relevant, the
next week delivers again.

Why writing is not on GET
-------------------------
Today GET must stay read-only (AIL.4A). ``preview`` (read-only) tells Today
which delivered prompts to show and whether allocation would deliver anything;
``allocate`` (write) is the separate, explicit, idempotent action the client
calls after loading Today. Calling it again changes nothing.

A candidate is a Concept that is DEMONSTRATED, REVIEW_DUE or REVIEW_FAILED,
and one a review can actually be started (or continued) for now — prompting a
review that cannot be started would waste the week's quota. No notification is
sent and nothing is scheduled.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from sqlalchemy import func

from app.models.concepts import Concept
from app.models.learner import LearningEvidence
from app.models.learning_review import ReviewPromptDelivery
from app.services.learner_state_service import LearnerConceptState, LearnerStateService
from app.services.review_attempt_service import ReviewAttemptService
from app.services.review_retention_service import _aware, review_kind

REVIEW_PROMPTS_PER_WEEK = 2
_SLOTS = (1, 2)
_ALLOCATION_RETRIES = 4

# Presentation kind ordering (must match review_retention_service._KIND_GROUP)
_KIND_GROUP = {"REVIEW_FAILED": 0, "CONCEPT_CHANGED_REVIEW": 1, "REVIEW_DUE": 2}


def week_start_of(moment: datetime) -> datetime:
    """Monday 00:00 UTC of the calendar week containing ``moment``."""
    utc = _aware(moment).astimezone(timezone.utc)
    monday = utc - timedelta(days=utc.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def prompt_sort_key_with_fairness(
    kind: str,
    due_at: Optional[datetime],
    name: str,
    concept_id: str,
    last_delivered_at: Optional[datetime],
    now: datetime,
) -> tuple:
    """Sort key that prevents starvation by rotating fairly through DUE concepts.

    FAILED and CONCEPT_CHANGED take priority: sort by oldest-due (they need attention).
    DUE: sort by least-recently-delivered (never-delivered first), then oldest-due,
         so recurring top concepts don't starve older due ones.
    """
    # FAILED and CONCEPT_CHANGED: keep them at the front, oldest-due first
    if kind != "REVIEW_DUE":
        return (
            _KIND_GROUP[kind],
            _aware(due_at).timestamp() if due_at else float("inf"),
            name.lower(),
            concept_id,
        )

    # DUE: rotate fairly among concepts
    # Priority: (1) never-delivered, (2) least-recently-delivered, (3) oldest-due
    weeks_since_delivery = float("inf")  # default: never delivered
    if last_delivered_at is not None:
        delta_seconds = (_aware(now) - _aware(last_delivered_at)).total_seconds()
        weeks_since = delta_seconds / (7 * 86400)
        weeks_since_delivery = weeks_since

    return (
        _KIND_GROUP[kind],
        weeks_since_delivery == float("inf"),  # False < True: delivered Concepts come after never-delivered
        weeks_since_delivery,  # ascending: least-recently-delivered first
        _aware(due_at).timestamp() if due_at else float("inf"),  # oldest-due within the group
        name.lower(),
        concept_id,
    )


@dataclass
class PromptCandidate:
    concept_id: str
    name: str
    kind: str
    state: LearnerConceptState


@dataclass
class PromptPreview:
    week_start: datetime
    limit: int
    delivered: List[ReviewPromptDelivery]
    not_prompted: int  # known due/failed Concepts that are not (or not yet) prompted
    pending: List[PromptCandidate] = field(default_factory=list)  # what allocate() would deliver now

    @property
    def allocation_pending(self) -> bool:
        return bool(self.pending)


@dataclass
class AllocationResult:
    week_start: datetime
    limit: int
    delivered: List[ReviewPromptDelivery]
    new: List[str]  # concept ids newly delivered by THIS call


class ReviewPromptService:
    def __init__(self, db: Session, *, now: Optional[datetime] = None):
        self.db = db
        self._now = _aware(now)
        self._learner_state = LearnerStateService(db)
        self._attempts = ReviewAttemptService(db, now=now)

    def _clock(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def week_start(self) -> datetime:
        return week_start_of(self._clock())

    # -- reads (never write) -------------------------------------------------------------

    def delivered(self, user_id: str) -> List[ReviewPromptDelivery]:
        """This user's prompts delivered in the current week, by slot."""
        return list(
            self.db.execute(
                select(ReviewPromptDelivery)
                .where(ReviewPromptDelivery.user_id == user_id, ReviewPromptDelivery.week_start == self.week_start())
                .order_by(ReviewPromptDelivery.slot)
            ).scalars()
        )

    def learned_states(self, user_id: str) -> Dict[str, LearnerConceptState]:
        concept_ids = self.db.execute(
            select(LearningEvidence.concept_id).where(LearningEvidence.user_id == user_id).distinct()
        ).scalars().all()
        return {cid: self._learner_state.state(user_id, cid, now=self._clock()) for cid in sorted(concept_ids)}

    def candidates(
        self, user_id: str, *, learned: Optional[Dict[str, LearnerConceptState]] = None
    ) -> List[PromptCandidate]:
        """Concepts worth prompting, in the deterministic prompt order.

        Ordering prevents starvation: DUE concepts rotate fairly by delivery history,
        while FAILED/CONCEPT_CHANGED take priority (oldest-due first).
        """
        learned = learned if learned is not None else self.learned_states(user_id)
        due = {
            cid: state
            for cid, state in learned.items()
            if state.review is not None and (state.review.due or state.review.failed)
        }
        if not due:
            return []
        names = dict(self.db.execute(select(Concept.id, Concept.name).where(Concept.id.in_(sorted(due)))).all())

        # Query delivery history to prevent starvation: for each concept, when was it last delivered?
        delivery_history = dict(
            self.db.execute(
                select(
                    ReviewPromptDelivery.concept_id,
                    func.max(ReviewPromptDelivery.delivered_at).label("last_delivered"),
                )
                .where(ReviewPromptDelivery.user_id == user_id)
                .group_by(ReviewPromptDelivery.concept_id)
            ).all()
        )

        found: List[PromptCandidate] = []
        for cid, state in due.items():
            if cid not in names:
                continue
            action = self._attempts.availability(user_id, cid, state=state).action
            if action not in ("start", "continue"):
                continue  # a review that cannot be started is not worth a prompt
            found.append(PromptCandidate(cid, names[cid], review_kind(state.review), state))

        # Sort with fairness: never-delivered concepts first, then least-recently-delivered
        return sorted(
            found,
            key=lambda c: prompt_sort_key_with_fairness(
                c.kind,
                c.state.review.due_at,
                c.name,
                c.concept_id,
                delivery_history.get(c.concept_id),
                self._clock(),
            ),
        )

    def preview(
        self, user_id: str, *, learned: Optional[Dict[str, LearnerConceptState]] = None
    ) -> PromptPreview:
        delivered = self.delivered(user_id)
        delivered_ids = {d.concept_id for d in delivered}
        undelivered = [c for c in self.candidates(user_id, learned=learned) if c.concept_id not in delivered_ids]
        free = max(0, REVIEW_PROMPTS_PER_WEEK - len(delivered))
        return PromptPreview(
            week_start=self.week_start(),
            limit=REVIEW_PROMPTS_PER_WEEK,
            delivered=delivered,
            not_prompted=len(undelivered),
            pending=undelivered[:free],
        )

    # -- the one write path -------------------------------------------------------------

    def allocate(self, user_id: str) -> AllocationResult:
        """Deliver, deterministically and idempotently, the prompts this user's
        week still has room for. Safe to call repeatedly: an already-delivered
        Concept is never delivered again in the same week, and a full week
        delivers nothing more."""
        week = self.week_start()
        newly: List[str] = []
        for _ in range(_ALLOCATION_RETRIES):
            delivered = self.delivered(user_id)
            used_slots = {d.slot for d in delivered}
            delivered_ids = {d.concept_id for d in delivered}
            free_slots = [slot for slot in _SLOTS if slot not in used_slots]
            if not free_slots:
                break
            pending = [c for c in self.candidates(user_id) if c.concept_id not in delivered_ids][: len(free_slots)]
            if not pending:
                break
            for candidate, slot in zip(pending, free_slots):
                self.db.add(
                    ReviewPromptDelivery(
                        user_id=user_id,
                        concept_id=candidate.concept_id,
                        week_start=week,
                        slot=slot,
                        prompt_kind=candidate.kind,
                        delivered_at=self._clock(),
                    )
                )
            try:
                self.db.commit()
            except (IntegrityError, OperationalError):
                # A concurrent allocation took a slot or the same Concept: the
                # database refused the extra row. Start over from what is stored.
                self.db.rollback()
                continue
            newly = [c.concept_id for c in pending]
            break
        return AllocationResult(
            week_start=week, limit=REVIEW_PROMPTS_PER_WEEK, delivered=self.delivered(user_id), new=newly
        )

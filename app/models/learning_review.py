"""Review attempts — AIL.4B, docs/ail-learning-spec-v1.md Sec 22.

A ``ReviewAttempt`` is bookkeeping for one retention review of a Concept. It is
NOT learning evidence and never becomes evidence by itself: a PASSED attempt
must point at a real ``learning_evidence`` row (enforced by the lifecycle
CHECK below), and that row is appended through the canonical Learning
Evidence path only after the review check satisfied the evidence-quality
contract (``app.services.review_attempt_service``).

Everything else about review — whether a Concept is REVIEW_DUE, whether the
latest review left REVIEW_FAILED, the current interval — is derived on read
(spec Sec 32.1 "derive, don't cache"; ``review_schedules`` stays deferred,
Sec 32.5). This table holds only the attempts themselves.

Conventions follow ``app.models.learner`` / ``app.models.lab``: UUID string
keys, ``sa_enum`` for the status, user-scoped via ``users.id`` (never
project-scoped), partial unique indexes (``sqlite_where``) where a plain
UNIQUE would misbehave with NULLs.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import ReviewAttemptStatus
from app.db.mixins import UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class ReviewAttempt(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "review_attempts"
    __table_args__ = (
        # The lifecycle is enforced by the database, not only by the service:
        # STARTED has no completion and no evidence; PASSED must have both
        # (so a PASSED row can never exist without real evidence); FAILED has
        # a completion time and never carries evidence.
        CheckConstraint(
            "(status = 'started' AND completed_at IS NULL AND resulting_learning_evidence_id IS NULL) OR "
            "(status = 'passed' AND completed_at IS NOT NULL AND resulting_learning_evidence_id IS NOT NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND resulting_learning_evidence_id IS NULL)",
            name="ck_review_attempts_lifecycle",
        ),
        Index("ix_review_attempts_user_concept_started", "user_id", "concept_id", "started_at"),
        # At most one in-progress review per user and Concept.
        Index(
            "uq_review_attempts_active",
            "user_id",
            "concept_id",
            unique=True,
            sqlite_where=text("status = 'started'"),
            postgresql_where=text("status = 'started'"),
        ),
        # One evidence row can result from at most one attempt.
        Index(
            "uq_review_attempts_resulting_evidence",
            "resulting_learning_evidence_id",
            unique=True,
            sqlite_where=text("resulting_learning_evidence_id IS NOT NULL"),
            postgresql_where=text("resulting_learning_evidence_id IS NOT NULL"),
        ),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False)
    # The exact ConceptVersion current when the review started — the version
    # the resulting evidence (if any) is recorded against.
    concept_version_id: Mapped[str] = mapped_column(ForeignKey("concept_versions.id"), nullable=False)
    learning_item_id: Mapped[Optional[str]] = mapped_column(ForeignKey("learning_items.id"), nullable=True)
    status: Mapped[ReviewAttemptStatus] = mapped_column(
        sa_enum(ReviewAttemptStatus), nullable=False, default=ReviewAttemptStatus.STARTED
    )
    resulting_learning_evidence_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("learning_evidence.id"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewPromptDelivery(UUIDPrimaryKeyMixin, Base):
    """One delivered review prompt — the persistence behind "at most two review
    prompts per week" (spec Sec 22.2, AIL.4 acceptance criterion 2).

    A delivery is the persisted, first-time surfacing of a review prompt for one
    Concept to one user in one UTC calendar week (Monday 00:00 UTC start). It is
    written only by the explicit allocation action
    (``app.services.review_prompt_service``), never by recomputing or reading
    Today, so refreshing Today cannot consume quota.

    The weekly cap is enforced by the database, not only by the service: ``slot``
    is 1 or 2 and unique per (user, week), so a third delivery in a week cannot
    exist even under concurrent allocation; and a Concept is delivered at most
    once per (user, week), so the same logical prompt is never counted twice. A
    prompt affects proactive prompting only — it never gates whether a learner
    may review.
    """

    __tablename__ = "review_prompt_deliveries"
    __table_args__ = (
        CheckConstraint("slot IN (1, 2)", name="ck_review_prompt_deliveries_slot"),
        UniqueConstraint("user_id", "week_start", "slot", name="uq_review_prompt_deliveries_user_week_slot"),
        UniqueConstraint(
            "user_id", "concept_id", "week_start", name="uq_review_prompt_deliveries_user_concept_week"
        ),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False)
    week_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    # REVIEW_DUE | REVIEW_FAILED | CONCEPT_CHANGED_REVIEW — why it was prompted
    # when it was delivered (the live card recomputes the current reason).
    prompt_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

"""Learner Profile / Interests / Plan / Evidence — AIL.1A, docs/ail-learning-
spec-v1.md Sec 17, 18.

Every table here is keyed directly off ``users.id`` — never
``project_id`` — per the AIL architecture reconciliation (frozen contract
#10): learner data is user-scoped, not project-scoped, and an org
Owner/Admin gets no visibility into it by virtue of that role alone. Every
repository/service method in ``app.services`` that touches these tables
takes an explicit ``user_id`` and filters on it.

``learner_profiles`` uses the codebase's universal synthetic-UUID primary
key (``UUIDPrimaryKeyMixin``) plus a unique ``user_id`` — not ``user_id``
as a natural primary key — for consistency with every other table in this
schema (including other 1:1-shaped tables such as
``app.models.identity.ProjectMembership``).

``learning_evidence`` is append-only: no update/delete method exists on
its repository/service except the full-user-deletion export/delete path.
A dispute or regrade appends a new row and points ``superseded_by_id`` at
it — the same "never delete, mark superseded" convention already used by
Radar's ``claims.superseded_by_id`` (spec Sec 26.3) — never an in-place
mutation of a graded row.

``learning_plan_items`` deliberately has no ``development_id`` column
(AIL architecture reconciliation, frozen contract #6): Radar's
``developments`` table does not exist in this slice, and adding an
unvalidated free-text reference to it would be worse than not having the
feature yet. Radar-originated plan proposals (``origin=radar``) arrive in
the coordinated AIL.1/AIL.2 integration migration that adds both the
column and the enum value together.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import (
    ConceptLevel,
    EvidenceRefType,
    EvidenceType,
    GradingMode,
    LearnerDepth,
    PlanItemOrigin,
    PlanItemState,
    QuestionOrigin,
)
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum

if TYPE_CHECKING:
    from app.models.concepts import LearningItem


class LearnerProfile(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "learner_profiles"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    level: Mapped[Optional[ConceptLevel]] = mapped_column(sa_enum(ConceptLevel), nullable=True)
    goal_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    depth: Mapped[Optional[LearnerDepth]] = mapped_column(sa_enum(LearnerDepth), nullable=True)
    weekly_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LearnerInterest(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Spec Sec 15.1/17.1 — interests are explicit only, never inferred
    from task content, code, prompts, artifacts, model outputs, or
    unrelated project activity (AIL architecture reconciliation)."""

    __tablename__ = "learner_interests"
    __table_args__ = (
        # Exactly one of term_id/concept_id. A plain UniqueConstraint over
        # both nullable columns would not prevent duplicates under SQLite
        # NULL semantics (each NULL compares distinct) — two partial
        # unique indexes, the same sqlite_where technique already used by
        # app.models.governance.Approval's
        # uq_approvals_workflow_node_run_operation, do the job instead.
        Index(
            "uq_learner_interests_user_id_term_id",
            "user_id",
            "term_id",
            unique=True,
            sqlite_where=text("term_id IS NOT NULL"),
        ),
        Index(
            "uq_learner_interests_user_id_concept_id",
            "user_id",
            "concept_id",
            unique=True,
            sqlite_where=text("concept_id IS NOT NULL"),
        ),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    term_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("taxonomy_terms.id", ondelete="CASCADE"), nullable=True
    )
    concept_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), nullable=True
    )
    watch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class LearningPlanItem(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Spec Sec 17.4/17.6 — user edits are never silently overwritten; a
    re-plan proposes a diff (``app.services.learning_plan_service``)."""

    __tablename__ = "learning_plan_items"
    __table_args__ = (
        # At most one active (proposed/planned) row per (user, concept) —
        # a concept may legitimately reappear in plan history after being
        # skipped/rejected, so this is intentionally partial, not a bare
        # UniqueConstraint on (user_id, concept_id).
        Index(
            "uq_learning_plan_items_user_id_concept_id_active",
            "user_id",
            "concept_id",
            unique=True,
            sqlite_where=text("state IN ('proposed', 'planned')"),
        ),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[PlanItemState] = mapped_column(
        sa_enum(PlanItemState), nullable=False, default=PlanItemState.PROPOSED
    )
    origin: Mapped[PlanItemOrigin] = mapped_column(sa_enum(PlanItemOrigin), nullable=False)


class LearningEvidence(UUIDPrimaryKeyMixin, Base):
    """Append-only — spec Sec 18.4 rule 3. No repository update/delete
    method exists for this table except the full-user-deletion path (spec
    Sec 30)."""

    __tablename__ = "learning_evidence"
    # AIL.3C: exactly one experiment-backed evidence row per user+experiment,
    # enforced by the database (mirrors the partial unique index in the AIL.3C
    # migration so create_all()-built test databases enforce it too).
    __table_args__ = (
        Index(
            "uq_learning_evidence_experiment_ref",
            "user_id",
            "ref_id",
            unique=True,
            sqlite_where=text("ref_type = 'experiment'"),
            postgresql_where=text("ref_type = 'experiment'"),
        ),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False)
    # The exact version this evidence was earned against — spec Sec 16.3 /
    # 18.4 rule 5 ("what's changed since I learned this?").
    concept_version_id: Mapped[str] = mapped_column(ForeignKey("concept_versions.id"), nullable=False)
    evidence_type: Mapped[EvidenceType] = mapped_column(sa_enum(EvidenceType), nullable=False)
    learning_item_id: Mapped[Optional[str]] = mapped_column(ForeignKey("learning_items.id"), nullable=True)
    # {"raw": ..., "max": ..., "pct": ...} — same non-numeric-single-score
    # shape as app.models.artifacts_eval.Evaluation.objective_metrics_json;
    # never a bare percentage column (spec Sec 21.2's "no bare percentages").
    score: Mapped[Optional[dict]] = mapped_column("score_json", nullable=True)
    passed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    grader: Mapped[GradingMode] = mapped_column(sa_enum(GradingMode), nullable=False)
    grader_confidence: Mapped[Optional[float]] = mapped_column(Numeric(5, 4), nullable=True)
    question_origin: Mapped[Optional[QuestionOrigin]] = mapped_column(sa_enum(QuestionOrigin), nullable=True)
    on_demo_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ref_type: Mapped[EvidenceRefType] = mapped_column(
        sa_enum(EvidenceRefType), nullable=False, default=EvidenceRefType.NONE
    )
    # Polymorphic per ref_type; no DB-level FK is possible across variable
    # target tables (same trade-off already accepted by
    # execution_events.artifact_refs_json / evaluation_criterion_results.
    # evidence_refs) — validated at the service layer instead.
    ref_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # A dispute/regrade appends a new row and points the disputed row here
    # — never an in-place mutation (see module docstring). Self-FK within
    # one table needs no use_alter: unlike a genuine cross-table cycle
    # (e.g. app.models.tasks.AgentRun.repair_of_review_id), SQLite can
    # create a self-referencing FK in a single CREATE TABLE statement.
    superseded_by_id: Mapped[Optional[str]] = mapped_column(ForeignKey("learning_evidence.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    learning_item: Mapped[Optional["LearningItem"]] = relationship()

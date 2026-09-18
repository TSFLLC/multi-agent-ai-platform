"""Evaluation Run execution — MA6 Slice 2.

Proves that an exact Agent output can be evaluated against an immutable
``EvaluationDefinitionVersion`` (app.models.evaluation_definitions) and
produce durable, structured, provenance-bound evidence — no scoring,
ranking, or winner concept anywhere in this module (same non-negotiable
invariant Slice 1 already documents).

``EvaluationRun`` binds to its subject Agent Run *and* the exact subject
Artifact, plus a defensive copy of that artifact's content hash frozen at
bind time — the same "freeze-at-decision-time" principle already used by
``app.models.reviews.AgentReview.candidate_artifact_hash`` and
``app.models.artifacts_eval.ComparisonRun.winner_artifact_hash``. Nothing
here ever mutates that hash after creation; a mismatch discovered at
execution time means the artifact changed underneath the bound Evaluation
Run and execution must refuse to proceed
(app.services.evaluation_execution_service).

Multiple Evaluation Runs may exist for the same subject Agent Run/Artifact
— each is its own immutable historical row; nothing ever overwrites a
prior Evaluation Run's criterion results.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import EvaluationFinding, EvaluationMethod, EvaluationRunStatus
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class EvaluationRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One evaluation attempt of a subject Agent Run's exact Artifact
    against one immutable ``EvaluationDefinitionVersion``. Manual-trigger
    only in Slice 2 — always created by an explicit operator request
    (app.api.routers.evaluation_runs), never enqueued automatically on
    Agent Run or MA5 candidate completion.
    """

    __tablename__ = "evaluation_runs"

    subject_agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    subject_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    # Defensive copy of artifacts.content_hash taken when this run was
    # created. NOT NULL (unlike Artifact.content_hash itself, which is
    # nullable): an artifact with no computed hash cannot be safely bound
    # for the exact-artifact protection this run exists to provide.
    subject_artifact_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # No ondelete: same as agent_runs.agent_version_id -- a registry
    # reference to an immutable, versioned row, never a cascade parent.
    evaluation_definition_version_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_definition_versions.id"), nullable=False
    )
    method: Mapped[EvaluationMethod] = mapped_column(sa_enum(EvaluationMethod), nullable=False)
    status: Mapped[EvaluationRunStatus] = mapped_column(
        sa_enum(EvaluationRunStatus), nullable=False, default=EvaluationRunStatus.PENDING
    )

    requested_by_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    criterion_results: Mapped[List["EvaluationCriterionResult"]] = relationship(
        back_populates="evaluation_run",
        passive_deletes=True,
        order_by="EvaluationCriterionResult.order_index",
    )


class EvaluationCriterionResult(UUIDPrimaryKeyMixin, Base):
    """One criterion's finding within one Evaluation Run — Slice 2 only
    ever writes MET/PARTIAL/NOT_MET/NOT_APPLICABLE (never a percentage,
    aggregate score, or rank; see ``app.db.enums.EvaluationFinding``).
    """

    __tablename__ = "evaluation_criterion_results"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_run_id", "criterion_key", name="uq_evaluation_criterion_results_run_id_key"
        ),
    )

    evaluation_run_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete: registry reference, same as evaluation_runs.
    # evaluation_definition_version_id above.
    evaluation_criterion_id: Mapped[str] = mapped_column(ForeignKey("evaluation_criteria.id"), nullable=False)
    # Defensive denormalized copy of evaluation_criteria.key/order_index --
    # same freeze-at-decision-time principle as the parent run's artifact
    # hash, so a result stays self-describing without a join even though
    # Slice 1's criteria are immutable and never expected to change
    # underneath it.
    criterion_key: Mapped[str] = mapped_column(String(80), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    finding: Mapped[EvaluationFinding] = mapped_column(sa_enum(EvaluationFinding), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    # Free-form references (e.g. artifact ids) supporting the finding --
    # never required, since NOT_APPLICABLE findings have nothing to cite.
    evidence_refs: Mapped[Optional[list]] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    evaluation_run: Mapped["EvaluationRun"] = relationship(back_populates="criterion_results")

"""Artifacts, Evaluation, Comparison — Section 17, 18, 24.2, 24.4 #7/#18.

``comparison_runs.winner_agent_run_id`` is a denormalized convenience FK
that must always agree with the ``comparison_candidates`` row where
``is_winner = True`` — enforced as an application invariant (SQLite cannot
express a cross-row consistency check declaratively), not a DB constraint.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import ArtifactType, ComparisonRunStatus, EvaluationHumanDecision, EvaluationJudgeSource
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class Artifact(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Durable output — Section 24.2, 24.4 #18 (content-integrity columns)."""

    __tablename__ = "artifacts"

    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[ArtifactType] = mapped_column(sa_enum(ArtifactType), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Evaluation(UUIDPrimaryKeyMixin, Base):
    """Section 17.3. Objective metrics are structurally separate from Judge
    output (ADR-4) — never merged into one score."""

    __tablename__ = "evaluations"

    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    objective_metrics: Mapped[Optional[dict]] = mapped_column("objective_metrics_json", nullable=True)
    # Provenance of each objective metric (which tool/check produced it) —
    # Section 18.3's "clear provenance" requirement.
    metric_provenance: Mapped[Optional[dict]] = mapped_column("metric_provenance_json", nullable=True)
    judge_score: Mapped[Optional[dict]] = mapped_column("judge_score_json", nullable=True)
    judge_source: Mapped[Optional[EvaluationJudgeSource]] = mapped_column(
        sa_enum(EvaluationJudgeSource), nullable=True
    )
    human_decision: Mapped[Optional[EvaluationHumanDecision]] = mapped_column(
        sa_enum(EvaluationHumanDecision), nullable=True
    )
    human_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ComparisonRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Section 18.1 grouping.

    MA5 clarification (reported per the Owner's "report the genuine
    blocker, make the minimum additive fix" instruction; no column change
    was needed here — see app.models.artifacts_eval module notes /
    MA5 completion report for the full writeup): the frozen ``task_run_id``
    column reads as if every candidate's Agent Run shared it directly, but
    MA3/MA4's execution engine finalizes *Task Run* status once, per run —
    N independently-terminal candidates (one COMPLETED, one FAILED, ...)
    cannot be represented by one shared Task Run's single status. Kept
    NOT NULL and fully populated, ``task_run_id`` now means "this
    comparison's own bookkeeping Task Run" — never executed against
    directly (no Agent Run ever points at it), used only as the anchor for
    comparison-level Flight Recorder events (reusing the existing
    Task Run-scoped events/stream endpoints verbatim) and as the shared
    ``budget_id`` every candidate's *own* independent Task Run (Task
    already documents "any number of Task Runs, including concurrent
    ones") is created with.
    """

    __tablename__ = "comparison_runs"

    task_run_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False)
    # AIL.3B experiment grouping; ordinary MA5 comparisons leave these NULL.
    experiment_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    experiment_task_position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    experiment_repetition: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[ComparisonRunStatus] = mapped_column(
        sa_enum(ComparisonRunStatus),
        nullable=False,
        default=ComparisonRunStatus.PENDING,
    )
    winner_agent_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    # MA5: the exact immutable artifact the human bound as canonical, plus
    # a defensive hash copy — same freeze-at-decision pattern as
    # app.models.reviews.AgentReview.candidate_artifact_hash /
    # task_runs.final_artifact_id (MA4) — "never silently change the
    # canonical result" even if winner_agent_run_id's artifact set ever
    # changed underneath it.
    winner_artifact_id: Mapped[Optional[str]] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    winner_artifact_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Cooperative cancellation (Section 26.7), same pattern as
    # task_runs.cancellation_requested_at — who requested it is on the
    # audit_events row emitted alongside, not duplicated here.
    cancellation_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    candidates: Mapped[List["ComparisonCandidate"]] = relationship(back_populates="comparison_run")


class ComparisonCandidate(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Normalized comparison membership — Section 24.4 #7.

    Replaces ``comparison_runs.candidate_agent_run_ids[]``.

    MA5 correction: ``agent_run_id`` was NOT NULL, assuming a candidate
    row is only ever created to describe an Agent Run that *already*
    exists (post-hoc grouping) — but MA5 candidates are *configured*
    (Agent Version, optional model override, optional MA4 review config)
    before they are launched, and ``agent_run_id`` only exists once
    launch actually creates that candidate's Agent Run. Relaxed to
    nullable; the new ``agent_version_id``/``model_policy_override_json``/
    ``review_config_json``/``task_run_id`` columns carry the configured
    execution recipe in the meantime. ``created_at`` (via
    ``CreatedAtMixin``, newly added) gives natural candidate ordering
    without a redundant explicit order column.
    """

    __tablename__ = "comparison_candidates"
    __table_args__ = (
        UniqueConstraint(
            "comparison_run_id", "agent_run_id", name="uq_comparison_candidates_run_id_agent_run_id"
        ),
        UniqueConstraint("comparison_run_id", "label", name="uq_comparison_candidates_run_id_label"),
    )

    comparison_run_id: Mapped[str] = mapped_column(
        ForeignKey("comparison_runs.id", ondelete="CASCADE"), nullable=False
    )
    agent_version_id: Mapped[str] = mapped_column(ForeignKey("agent_versions.id"), nullable=False)
    # Per-candidate model override (Section 3: "same Agent, different
    # models" must be expressible) — an immutable, published Agent
    # Version's own model_policy is shared/frozen, so a candidate that
    # wants a *different* concrete model than its Agent Version's default
    # stores that override here; copied onto the candidate's Agent Run
    # (app.models.tasks.AgentRun.model_policy_override_json) at launch,
    # which app.services.execution_service._resolve_model prefers over
    # the Agent Version's own policy when present. NULL means "use the
    # Agent Version's own model_policy, unmodified."
    model_policy_override_json: Mapped[Optional[dict]] = mapped_column(nullable=True)
    # Optional MA4 review configuration for this candidate only (Section
    # 11) — {"reviewer_agent_version_id": ..., "max_repair_iterations": ...}.
    # Never required; most candidates leave this NULL (plain single-Agent).
    review_config_json: Mapped[Optional[dict]] = mapped_column(nullable=True)
    # Set at launch time once this candidate's own Task Run is created.
    task_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("task_runs.id"), nullable=True)
    # Set at launch time once this candidate's (first/primary) Agent Run
    # is created — nullable until then, see class docstring.
    agent_run_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_winner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    comparison_run: Mapped["ComparisonRun"] = relationship(back_populates="candidates")

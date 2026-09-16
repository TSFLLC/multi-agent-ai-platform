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
    """Section 18.1 grouping."""

    __tablename__ = "comparison_runs"

    task_run_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[ComparisonRunStatus] = mapped_column(
        sa_enum(ComparisonRunStatus),
        nullable=False,
        default=ComparisonRunStatus.PENDING,
    )
    winner_agent_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    candidates: Mapped[List["ComparisonCandidate"]] = relationship(back_populates="comparison_run")


class ComparisonCandidate(UUIDPrimaryKeyMixin, Base):
    """Normalized comparison membership — Section 24.4 #7.

    Replaces ``comparison_runs.candidate_agent_run_ids[]``.
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
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_winner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    comparison_run: Mapped["ComparisonRun"] = relationship(back_populates="candidates")

"""AIL.3 Personal Lab foundation: Test Kits and draft Experiments."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import CostEstimateKind, EvalSetVersionStatus, ExperimentStatus, ExperimentType
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class EvalSet(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "eval_sets"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_eval_sets_user_id_name"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role_term_id: Mapped[Optional[str]] = mapped_column(ForeignKey("taxonomy_terms.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class EvalSetVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "eval_set_versions"
    __table_args__ = (UniqueConstraint("eval_set_id", "version", name="uq_eval_set_versions_set_id_version"),)

    eval_set_id: Mapped[str] = mapped_column(ForeignKey("eval_sets.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[EvalSetVersionStatus] = mapped_column(
        sa_enum(EvalSetVersionStatus), nullable=False, default=EvalSetVersionStatus.DRAFT
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    frozen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class EvalSetVersionTask(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "eval_set_version_tasks"
    __table_args__ = (
        UniqueConstraint("eval_set_version_id", "position", name="uq_eval_set_version_tasks_position"),
        UniqueConstraint("eval_set_version_id", "task_id", name="uq_eval_set_version_tasks_task"),
        Index("ix_eval_set_version_tasks_version_id", "eval_set_version_id"),
    )

    eval_set_version_id: Mapped[str] = mapped_column(
        ForeignKey("eval_set_versions.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    task_snapshot: Mapped[dict] = mapped_column("task_snapshot_json", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Experiment(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "experiments"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    experiment_type: Mapped[ExperimentType] = mapped_column(sa_enum(ExperimentType), nullable=False)
    status: Mapped[ExperimentStatus] = mapped_column(
        sa_enum(ExperimentStatus), nullable=False, default=ExperimentStatus.DRAFT
    )
    hypothesis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    eval_set_version_id: Mapped[Optional[str]] = mapped_column(ForeignKey("eval_set_versions.id"), nullable=True)
    development_id: Mapped[Optional[str]] = mapped_column(ForeignKey("developments.id"), nullable=True)
    concept_id: Mapped[Optional[str]] = mapped_column(ForeignKey("concepts.id"), nullable=True)
    learning_item_id: Mapped[Optional[str]] = mapped_column(ForeignKey("learning_items.id"), nullable=True)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config_snapshot: Mapped[dict] = mapped_column("config_snapshot_json", nullable=False)
    estimated_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    estimated_currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    cost_estimate_kind: Mapped[CostEstimateKind] = mapped_column(
        sa_enum(CostEstimateKind), nullable=False, default=CostEstimateKind.UNKNOWN
    )
    budget_id: Mapped[Optional[str]] = mapped_column(ForeignKey("budgets.id"), nullable=True)
    approval_id: Mapped[Optional[str]] = mapped_column(ForeignKey("approvals.id"), nullable=True)
    frozen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    conclusion_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    conclusion_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    concluded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ExperimentAgentVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "experiment_agent_versions"
    __table_args__ = (
        UniqueConstraint("experiment_id", "agent_version_id", name="uq_experiment_agent_versions_pair"),
    )

    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False)
    agent_version_id: Mapped[str] = mapped_column(ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ExperimentModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "experiment_models"
    __table_args__ = (
        UniqueConstraint("experiment_id", "provider_model_snapshot_id", name="uq_experiment_models_snapshot"),
    )

    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id", ondelete="RESTRICT"), nullable=False)
    provider_model_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("provider_model_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ExperimentTaskRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Minimal reproducibility index for every TaskRun launched by an experiment."""

    __tablename__ = "experiment_task_runs"
    __table_args__ = (
        UniqueConstraint("experiment_id", "task_run_id", name="uq_experiment_task_runs_pair"),
        UniqueConstraint("experiment_id", "task_position", "repetition", "label", name="uq_experiment_task_runs_slot"),
    )

    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True)
    task_run_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False)
    task_position: Mapped[int] = mapped_column(Integer, nullable=False)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)

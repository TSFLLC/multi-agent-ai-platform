"""Approvals, Budgets/Reservations, Usage, Secret References.

Section 19 (Cost/Budget), 23 (Approval), 24.4 #13/#15/#19, 20.1/20.2
(secrets never stored by value in ordinary domain tables).
"""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import ApprovalScope, ApprovalStatus, BudgetReservationStatus, BudgetScope, UsageSourceType
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class Approval(UUIDPrimaryKeyMixin, Base):
    """Section 23.3, 24.4 #15 — exact action binding.

    ``action_fingerprint`` (sha256 of the exact payload being approved) and
    ``bound_artifact_id`` close the TOCTOU-style approval-bypass gap: the
    gated transition's execution code must recompute the current action's
    fingerprint and reject the transition if it no longer matches this
    row's ``action_fingerprint`` exactly (Acceptance Criterion 12).

    Decision provenance (``status``, ``resolved_by``, ``resolved_at``,
    ``resolution_note``) is written exactly once, by a compare-and-swap on
    ``status = 'pending'`` in ``ApprovalService.resolve`` -- a resolved row is
    never updated again.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        # MA7.3a: at most one approval per (workflow node run, operation).
        # Partial on purpose -- other scopes (task/agent run, artifact) may
        # legitimately re-request an approval for the same reference. Bare
        # index name used verbatim (the "ix" naming convention has no
        # ``%(constraint_name)s`` placeholder, see app.db.base).
        Index(
            "uq_approvals_workflow_node_run_operation",
            "scope",
            "scope_ref_id",
            "operation_type",
            unique=True,
            sqlite_where=text("scope = 'workflow_node_run'"),
        ),
    )

    scope: Mapped[ApprovalScope] = mapped_column(sa_enum(ApprovalScope), nullable=False)
    scope_ref_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(120), nullable=False)
    policy_rule_id: Mapped[Optional[str]] = mapped_column(ForeignKey("policy_rules.id"), nullable=True)
    action_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    bound_artifact_id: Mapped[Optional[str]] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    status: Mapped[ApprovalStatus] = mapped_column(
        sa_enum(ApprovalStatus),
        nullable=False,
        default=ApprovalStatus.PENDING,
    )
    requested_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    resolved_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Budget(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Section 19.1. Thresholds default to the frozen 80/95/100 contract."""

    __tablename__ = "budgets"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    scope: Mapped[BudgetScope] = mapped_column(sa_enum(BudgetScope), nullable=False)
    scope_ref_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    threshold_warn_pct: Mapped[int] = mapped_column(default=80)
    threshold_critical_pct: Mapped[int] = mapped_column(default=95)
    threshold_hard_pct: Mapped[int] = mapped_column(default=100)

    reservations: Mapped[List["BudgetReservation"]] = relationship(back_populates="budget")


class BudgetReservation(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Section 24.4 #13 — correctness under concurrent/parallel execution.

    A reservation for the *estimated* cost of a Model Call/Agent Run is
    created before dispatch (one short transaction); on completion, actual
    usage is recorded and the reservation is committed (adjusted to actual)
    or released. Threshold checks (Section 19.3) are evaluated against
    reserved + actual, never actual alone — this is what closes the
    check-then-act race across N parallel Mode 3 candidates.
    """

    __tablename__ = "budget_reservations"

    budget_id: Mapped[str] = mapped_column(ForeignKey("budgets.id", ondelete="CASCADE"), nullable=False)
    agent_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    task_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("task_runs.id"), nullable=True)
    reserved_amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    committed_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    status: Mapped[BudgetReservationStatus] = mapped_column(
        sa_enum(BudgetReservationStatus),
        nullable=False,
        default=BudgetReservationStatus.ACTIVE,
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    budget: Mapped["Budget"] = relationship(back_populates="reservations")


class UsageEvent(UUIDPrimaryKeyMixin, Base):
    """Section 19.2. Rolls up into budget totals via ``usage_event_budgets``
    (a normalized join table, replacing the v1.0 draft's
    ``usage_events.budget_ids[]`` array — one usage event, e.g. a single
    Model Call, can count against several simultaneously-applicable budget
    scopes such as execution+day+month)."""

    __tablename__ = "usage_events"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[UsageSourceType] = mapped_column(sa_enum(UsageSourceType), nullable=False)
    source_ref_id: Mapped[str] = mapped_column(String(36), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    budget_allocations: Mapped[List["UsageEventBudget"]] = relationship(back_populates="usage_event")


class UsageEventBudget(UUIDPrimaryKeyMixin, Base):
    """Normalized usage-event <-> budget allocation join table."""

    __tablename__ = "usage_event_budgets"
    __table_args__ = (
        UniqueConstraint("usage_event_id", "budget_id", name="uq_usage_event_budgets_event_id_budget_id"),
    )

    usage_event_id: Mapped[str] = mapped_column(
        ForeignKey("usage_events.id", ondelete="CASCADE"), nullable=False
    )
    budget_id: Mapped[str] = mapped_column(ForeignKey("budgets.id", ondelete="CASCADE"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)

    usage_event: Mapped["UsageEvent"] = relationship(back_populates="budget_allocations")


class SecretReference(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Section 24.4 #19 — never a plaintext value in an ordinary row.

    ``secret_store_ref`` is an opaque pointer into the actual local secret
    store (OS keychain preferred where practical, per Owner instruction).
    The mechanism behind the pointer is a later-phase (MA2) implementation
    detail; the pattern (reference, not value, in domain tables) is frozen
    now.
    """

    __tablename__ = "secret_references"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    provider_id: Mapped[Optional[str]] = mapped_column(ForeignKey("providers.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_store_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)

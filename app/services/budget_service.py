"""Cost / Budget Governor — Section 19, 24.4 #13, MA3.

The essential MA3 guarantee: an execution must not knowingly exceed a
hard budget without authorization. Reservations are created for the
*estimated* cost before invocation (closing the check-then-act race
Section 24.4 #13 already designed for) and settled or released once the
actual outcome is known. Not MA8 optimization — a small, deterministic
heuristic, never a fabricated "$0.00" for a cost that isn't actually zero
or known.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.enums import BudgetReservationStatus, PricingClassification, UsageSourceType
from app.model_resolution import ResolvedModel
from app.models.governance import Budget, BudgetReservation, UsageEvent, UsageEventBudget


class BudgetExceededError(Exception):
    """Raised when proceeding would push a budget's reserved+actual spend
    to or past its hard (100%) threshold — the run must not proceed."""


@dataclass
class CostEstimate:
    amount: Decimal
    is_estimated: bool


def estimate_cost(resolved: ResolvedModel) -> CostEstimate:
    """Never fabricates $0.00: FREE is a *verified* zero (not an
    estimate); UNKNOWN pricing gets a conservative flat reserve
    (settings.unknown_pricing_reserve_amount) specifically so an unpriced
    model can never bypass budget enforcement by looking free."""
    if resolved.pricing_classification == PricingClassification.FREE:
        return CostEstimate(amount=Decimal(0), is_estimated=False)

    pm = resolved.provider_model
    if pm.cost_input_per_mtok is None or pm.cost_output_per_mtok is None:
        return CostEstimate(amount=Decimal(settings.unknown_pricing_reserve_amount), is_estimated=True)

    tokens_in = Decimal(settings.budget_estimate_tokens_in)
    tokens_out = Decimal(settings.budget_estimate_tokens_out)
    amount = (tokens_in / Decimal(1_000_000)) * pm.cost_input_per_mtok + (
        tokens_out / Decimal(1_000_000)
    ) * pm.cost_output_per_mtok
    return CostEstimate(amount=amount, is_estimated=True)


class BudgetGovernor:
    def __init__(self, db: Session):
        self.db = db

    def get_budget(self, budget_id: Optional[str]) -> Optional[Budget]:
        if budget_id is None:
            return None
        return self.db.get(Budget, budget_id)

    def _current_committed_and_active_total(self, budget: Budget) -> Decimal:
        stmt = select(BudgetReservation).where(
            BudgetReservation.budget_id == budget.id,
            BudgetReservation.status.in_([BudgetReservationStatus.ACTIVE, BudgetReservationStatus.COMMITTED]),
        )
        total = Decimal(0)
        for reservation in self.db.execute(stmt).scalars().all():
            if (
                reservation.status == BudgetReservationStatus.COMMITTED
                and reservation.committed_amount is not None
            ):
                total += reservation.committed_amount
            else:
                total += reservation.reserved_amount
        return total

    def reserve(
        self,
        *,
        budget: Optional[Budget],
        agent_run_id: Optional[str],
        task_run_id: Optional[str],
        amount: Decimal,
    ) -> Optional[BudgetReservation]:
        """No budget configured for this Task Run -> no reservation, no
        enforcement (an explicit, documented V1 posture — Section 19
        scoping — not an oversight)."""
        if budget is None:
            return None

        existing_total = self._current_committed_and_active_total(budget)
        projected_pct = (
            ((existing_total + amount) / budget.limit_amount) * 100 if budget.limit_amount else Decimal(0)
        )

        if projected_pct >= budget.threshold_hard_pct:
            raise BudgetExceededError(
                f"Budget {budget.id} would reach {projected_pct:.1f}% "
                f"(hard threshold {budget.threshold_hard_pct}%) — refusing to proceed."
            )

        reservation = BudgetReservation(
            budget_id=budget.id,
            agent_run_id=agent_run_id,
            task_run_id=task_run_id,
            reserved_amount=amount,
            status=BudgetReservationStatus.ACTIVE,
        )
        self.db.add(reservation)
        self.db.commit()
        self.db.refresh(reservation)
        return reservation

    def threshold_state(self, budget: Budget) -> str:
        """Returns "ok" | "warn" | "critical" | "hard" for the budget's
        *current* committed+active total (after a reservation has already
        been made) — callers use this to decide whether to emit a Flight
        Recorder warning event."""
        total = self._current_committed_and_active_total(budget)
        pct = (total / budget.limit_amount) * 100 if budget.limit_amount else Decimal(0)
        if pct >= budget.threshold_hard_pct:
            return "hard"
        if pct >= budget.threshold_critical_pct:
            return "critical"
        if pct >= budget.threshold_warn_pct:
            return "warn"
        return "ok"

    def settle(self, reservation: Optional[BudgetReservation], *, actual_amount: Decimal) -> None:
        if reservation is None:
            return
        reservation.status = BudgetReservationStatus.COMMITTED
        reservation.committed_amount = actual_amount
        self.db.commit()

    def release(self, reservation: Optional[BudgetReservation]) -> None:
        """Used on provider failure/cancellation/timeout — the estimated
        spend never happened, so the reservation must not linger and
        count against future budget checks."""
        if reservation is None:
            return
        reservation.status = BudgetReservationStatus.RELEASED
        self.db.commit()

    def record_usage(
        self,
        *,
        project_id: str,
        budget: Optional[Budget],
        amount: Decimal,
        source_type: UsageSourceType,
        source_ref_id: str,
    ) -> UsageEvent:
        usage = UsageEvent(
            project_id=project_id,
            source_type=source_type,
            source_ref_id=source_ref_id,
            amount=amount,
            occurred_at=datetime.now(timezone.utc),
        )
        self.db.add(usage)
        self.db.flush()
        if budget is not None:
            self.db.add(UsageEventBudget(usage_event_id=usage.id, budget_id=budget.id, amount=amount))
        self.db.commit()
        self.db.refresh(usage)
        return usage

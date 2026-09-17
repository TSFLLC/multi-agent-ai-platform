"""Cost / Budget Governor — Section 19, 24.4 #13, MA3."""

from decimal import Decimal

import pytest

from app.db.enums import BudgetReservationStatus, PricingClassification, UsageSourceType
from app.model_resolution import ResolvedModel
from app.services.budget_service import BudgetExceededError, BudgetGovernor, estimate_cost
from tests.conftest import make_budget, make_provider_model


def _resolved(pm, classification):
    return ResolvedModel(
        provider_model=pm,
        model=None,
        provider=None,
        pricing_classification=classification,
        rationale="test",
        eligible_candidate_ids=[pm.id],
    )


def test_estimate_cost_free_is_verified_zero_not_estimated(db):
    pm = make_provider_model(db, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
    estimate = estimate_cost(_resolved(pm, PricingClassification.FREE))
    assert estimate.amount == Decimal(0)
    assert estimate.is_estimated is False


def test_estimate_cost_unknown_pricing_uses_conservative_reserve_not_zero(db):
    pm = make_provider_model(db, cost_input_per_mtok=None, cost_output_per_mtok=None)
    estimate = estimate_cost(_resolved(pm, PricingClassification.UNKNOWN))
    assert estimate.amount > Decimal(0)
    assert estimate.is_estimated is True


def test_estimate_cost_paid_computes_from_token_heuristic(db):
    pm = make_provider_model(db, cost_input_per_mtok=Decimal("3.00"), cost_output_per_mtok=Decimal("6.00"))
    estimate = estimate_cost(_resolved(pm, PricingClassification.PAID))
    assert estimate.amount > Decimal(0)
    assert estimate.is_estimated is True


def test_reserve_returns_none_when_no_budget_configured(db):
    governor = BudgetGovernor(db)
    reservation = governor.reserve(budget=None, agent_run_id="x", task_run_id="y", amount=Decimal("5.00"))
    assert reservation is None


def test_reserve_creates_active_reservation(db):
    budget = make_budget(db, limit_amount="10.00")
    db.commit()
    governor = BudgetGovernor(db)
    reservation = governor.reserve(budget=budget, agent_run_id=None, task_run_id=None, amount=Decimal("1.00"))
    assert reservation is not None
    assert reservation.status == BudgetReservationStatus.ACTIVE
    assert reservation.reserved_amount == Decimal("1.00")


def test_reserve_rejects_when_it_would_reach_hard_threshold(db):
    budget = make_budget(db, limit_amount="10.00")
    db.commit()
    governor = BudgetGovernor(db)
    with pytest.raises(BudgetExceededError):
        governor.reserve(budget=budget, agent_run_id=None, task_run_id=None, amount=Decimal("10.00"))


def test_settle_commits_actual_amount(db):
    budget = make_budget(db, limit_amount="10.00")
    db.commit()
    governor = BudgetGovernor(db)
    reservation = governor.reserve(budget=budget, agent_run_id=None, task_run_id=None, amount=Decimal("2.00"))
    governor.settle(reservation, actual_amount=Decimal("1.50"))
    db.refresh(reservation)
    assert reservation.status == BudgetReservationStatus.COMMITTED
    assert reservation.committed_amount == Decimal("1.50")


def test_release_frees_reservation_from_future_budget_checks(db):
    budget = make_budget(db, limit_amount="10.00")
    db.commit()
    governor = BudgetGovernor(db)
    reservation = governor.reserve(budget=budget, agent_run_id=None, task_run_id=None, amount=Decimal("9.00"))
    governor.release(reservation)
    db.refresh(reservation)
    assert reservation.status == BudgetReservationStatus.RELEASED

    # A released reservation must not count against a subsequent check.
    second = governor.reserve(budget=budget, agent_run_id=None, task_run_id=None, amount=Decimal("9.00"))
    assert second is not None


def test_threshold_state_transitions(db):
    budget = make_budget(db, limit_amount="10.00")
    db.commit()
    governor = BudgetGovernor(db)
    assert governor.threshold_state(budget) == "ok"

    governor.reserve(budget=budget, agent_run_id=None, task_run_id=None, amount=Decimal("8.50"))
    assert governor.threshold_state(budget) == "warn"


def test_record_usage_creates_usage_event(db):
    budget = make_budget(db, limit_amount="10.00")
    db.commit()
    governor = BudgetGovernor(db)
    usage = governor.record_usage(
        project_id=budget.project_id,
        budget=budget,
        amount=Decimal("0.50"),
        source_type=UsageSourceType.MODEL_CALL,
        source_ref_id="model-call-1",
    )
    assert usage.amount == Decimal("0.50")

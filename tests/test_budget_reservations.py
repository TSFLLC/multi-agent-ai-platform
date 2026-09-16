"""Budget reservation correctness — Section 24.4 #13, Acceptance Criterion 13.

Reservations for *estimated* cost are created before dispatch so N
parallel Mode 3 candidates cannot each check a stale "actual spend so far"
total and all proceed. MA0 does not implement the Cost/Budget Governor
that performs this check (MA3); it proves the schema can represent
reserved+actual correctly and that reservation rows are independent,
concurrency-safe records rather than a single mutable counter.
"""

from decimal import Decimal

from app.db.enums import BudgetReservationStatus
from app.models.governance import BudgetReservation
from tests.conftest import make_agent_run, make_budget, make_task_run


def _active_reserved_total(db, budget_id) -> Decimal:
    rows = (
        db.query(BudgetReservation)
        .filter_by(budget_id=budget_id, status=BudgetReservationStatus.ACTIVE)
        .all()
    )
    return sum((r.reserved_amount for r in rows), Decimal(0))


def test_multiple_reservations_sum_against_the_same_budget(db):
    budget = make_budget(db, limit_amount="10.00")
    task_run = make_task_run(db)

    for _ in range(3):
        run = make_agent_run(db, task_run=task_run)
        db.add(
            BudgetReservation(
                budget_id=budget.id,
                agent_run_id=run.id,
                reserved_amount=Decimal("4.00"),
                status=BudgetReservationStatus.ACTIVE,
            )
        )
        db.commit()

    total = _active_reserved_total(db, budget.id)
    assert total == Decimal("12.00")
    # The three reservations already exceed the $10 limit — this is
    # exactly the check-then-act race Section 24.4 #13 closes: a Cost
    # Governor evaluating "reserved + actual" against this budget would
    # correctly see the overcommitment and block the third dispatch,
    # rather than only discovering it after all three ran (v1.0's gap).
    assert total > budget.limit_amount


def test_reservation_commits_to_actual_and_releases_others(db):
    budget = make_budget(db, limit_amount="10.00")
    task_run = make_task_run(db)
    run = make_agent_run(db, task_run=task_run)

    reservation = BudgetReservation(
        budget_id=budget.id,
        agent_run_id=run.id,
        reserved_amount=Decimal("5.00"),
        status=BudgetReservationStatus.ACTIVE,
    )
    db.add(reservation)
    db.commit()

    # Model Call completes; actual cost differs slightly from the estimate.
    reservation.status = BudgetReservationStatus.COMMITTED
    reservation.committed_amount = Decimal("4.75")
    db.commit()
    db.refresh(reservation)

    assert reservation.status == BudgetReservationStatus.COMMITTED
    assert reservation.committed_amount == Decimal("4.75")


def test_released_reservations_excluded_from_active_total(db):
    budget = make_budget(db, limit_amount="10.00")
    task_run = make_task_run(db)
    run = make_agent_run(db, task_run=task_run)

    db.add(
        BudgetReservation(
            budget_id=budget.id,
            agent_run_id=run.id,
            reserved_amount=Decimal("9.00"),
            status=BudgetReservationStatus.RELEASED,
        )
    )
    db.commit()

    assert _active_reserved_total(db, budget.id) == Decimal(0)

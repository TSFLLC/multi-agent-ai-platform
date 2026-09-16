"""Cost / Budget Governor — Section 11, 19. Tracks usage against budgets,
applies the graduated 80/95/100 policy response, blocks/requires approval
at limits. Real implementation starts MA3 (cross-cutting, per the MA7
ordering note — not deferred to a single later phase)."""

from app.services.base import BaseService


class CostBudgetGovernor(BaseService):
    def reserve(self, *args, **kwargs):
        """Creates a budget_reservations row for estimated cost *before*
        dispatch (Section 24.4 #13) — closes the Mode 3 check-then-act
        race. Threshold checks must read reserved + actual, never actual
        alone."""
        raise NotImplementedError("CostBudgetGovernor lands in MA3")

    def commit_or_release(self, *args, **kwargs):
        raise NotImplementedError("CostBudgetGovernor lands in MA3")

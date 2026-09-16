from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from app.db.enums import BudgetScope


class BudgetCreate(BaseModel):
    project_id: str
    scope: BudgetScope
    scope_ref_id: Optional[str] = None
    limit_amount: Decimal
    currency: str = "USD"
    threshold_warn_pct: int = 80
    threshold_critical_pct: int = 95
    threshold_hard_pct: int = 100


class BudgetRead(BaseModel):
    id: str
    project_id: str
    scope: BudgetScope
    limit_amount: Decimal
    currency: str
    threshold_warn_pct: int
    threshold_critical_pct: int
    threshold_hard_pct: int


class UsageRead(BaseModel):
    id: str
    project_id: str
    source_type: str
    amount: Decimal
    currency: str

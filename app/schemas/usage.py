from datetime import datetime
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


class ModelCallRead(BaseModel):
    """Usage/cost accounting view of one Model Call (Section 6 "Model
    calls" contract) — ``cost_is_estimated`` makes explicit whether
    ``cost_amount`` is a real provider-reported/priced figure or a
    conservative estimate, never an ambiguous number (Acceptance
    Criterion 4)."""

    id: str
    agent_run_id: str
    model_id: str
    provider_id: str
    provider_model_snapshot_id: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cost_amount: Optional[Decimal] = None
    cost_currency: str
    cost_is_estimated: bool
    latency_ms: Optional[int] = None
    provider_request_id: Optional[str] = None
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None

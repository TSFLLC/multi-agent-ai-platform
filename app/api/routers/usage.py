"""Cost/Budget resource boundary — Section 25.2, 19."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.schemas.usage import BudgetCreate, BudgetRead, UsageRead

router = APIRouter(tags=["usage"])


@router.get("/usage", response_model=List[UsageRead])
def get_usage(
    db: Session = Depends(get_db),
    scope: Optional[str] = Query(default="project"),
    range: Optional[str] = Query(default=None),
):
    not_implemented()


@router.get("/costs/budgets", response_model=List[BudgetRead])
def list_budgets(db: Session = Depends(get_db)):
    not_implemented()


@router.post("/budgets", response_model=BudgetRead, status_code=201)
def create_budget(body: BudgetCreate, db: Session = Depends(get_db)):
    """Thresholds default to the frozen 80/95/100 contract (Owner decision)."""
    not_implemented()


@router.patch("/budgets/{budget_id}", response_model=BudgetRead)
def update_budget(budget_id: str, db: Session = Depends(get_db)):
    not_implemented()

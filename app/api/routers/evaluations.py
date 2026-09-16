"""Evaluation resource boundary — Section 25.2, 17."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.schemas.evaluations import EvaluationRead

router = APIRouter(tags=["evaluations"])


@router.get("/evaluations", response_model=List[EvaluationRead])
def list_evaluations(
    db: Session = Depends(get_db),
    agent_id: Optional[str] = Query(default=None),
    model_id: Optional[str] = Query(default=None),
    task_type: Optional[str] = Query(default=None),
):
    """Cross-axis query per Section 17.3. Sample-size-aware "insufficient
    data" semantics (Acceptance Criterion 10) belong to MA6's query
    service, not this contract stub."""
    not_implemented()


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationRead)
def get_evaluation(evaluation_id: str, db: Session = Depends(get_db)):
    not_implemented()

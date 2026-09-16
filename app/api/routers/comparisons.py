"""Comparison resource boundary — Section 25.2, 25.5, 18."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.schemas.comparisons import ComparisonRunCreate, ComparisonRunRead, SelectWinnerRequest

router = APIRouter(tags=["comparisons"])


@router.post("/comparisons", response_model=ComparisonRunRead, status_code=201)
def create_comparison(body: ComparisonRunCreate, db: Session = Depends(get_db)):
    """Candidates are {agent_run_id, label} objects backed by
    comparison_candidates (Section 24.4 #7) — never a bare agent_run_ids
    array."""
    not_implemented()


@router.get("/comparisons/{comparison_id}", response_model=ComparisonRunRead)
def get_comparison(comparison_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.post("/comparisons/{comparison_id}/select-winner", response_model=ComparisonRunRead)
def select_comparison_winner(comparison_id: str, body: SelectWinnerRequest, db: Session = Depends(get_db)):
    """Sets is_winner on the corresponding comparison_candidates row and
    syncs the denormalized comparison_runs.winner_agent_run_id — the two
    must never disagree (application invariant)."""
    not_implemented()

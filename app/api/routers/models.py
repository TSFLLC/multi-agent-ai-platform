"""Model/Provider Registry resource boundary — Section 25.2, 13, 14.5.

``pricing`` filter values mirror the Owner's FREE/PAID/ANY requirement
(Section 3) at the contract level; MA0 does not implement the query logic
behind it.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.db.enums import PricingClassification
from app.schemas.providers import ModelRead, ProviderModelRead, ProviderRead, RouterSimulateResponse

router = APIRouter(tags=["models"])


@router.get("/models", response_model=List[ModelRead])
def list_models(
    db: Session = Depends(get_db),
    pricing: Optional[PricingClassification] = Query(
        default=None, description="Filter by FREE/PAID/UNKNOWN pricing classification."
    ),
):
    not_implemented()


@router.get("/models/{model_id}", response_model=ModelRead)
def get_model(model_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/models/{model_id}/provider-models", response_model=List[ProviderModelRead])
def list_provider_models(model_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.post("/models/refresh", status_code=202)
def refresh_models(db: Session = Depends(get_db)):
    """Triggers a Provider Adapter catalog refresh (Section 13.3). No
    OpenRouterAdapter exists yet in MA0 — this endpoint exists for contract
    completeness only."""
    not_implemented()


@router.get("/providers", response_model=List[ProviderRead])
def list_providers(db: Session = Depends(get_db)):
    not_implemented()


@router.post("/providers", response_model=ProviderRead, status_code=201)
def create_provider(db: Session = Depends(get_db)):
    not_implemented()


@router.get("/router/simulate", response_model=RouterSimulateResponse)
def simulate_router(db: Session = Depends(get_db)):
    """Section 25.5 dry-run — must return the same
    router_policy_version_id/eligible_candidates shape a real routing
    decision would record. No Router exists yet in MA0."""
    not_implemented()

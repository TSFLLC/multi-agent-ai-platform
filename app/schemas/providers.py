from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from app.db.enums import HealthStatus, ModelStatus, PricingClassification, ProviderType


class ProviderRead(BaseModel):
    id: str
    type: ProviderType
    name: str
    health_status: HealthStatus


class ModelRead(BaseModel):
    id: str
    canonical_model_id: str
    family: Optional[str] = None
    context_window: Optional[int] = None
    status: ModelStatus


class ProviderModelRead(BaseModel):
    """Section 3 free-model requirement: ``pricing_classification`` is always
    present and is derived (FREE/PAID/UNKNOWN), never a hard-coded list
    lookup — see app.domain.pricing.classify_pricing."""

    id: str
    model_id: str
    provider_id: str
    provider_model_id: str
    cost_input_per_mtok: Optional[Decimal] = None
    cost_output_per_mtok: Optional[Decimal] = None
    currency: str
    pricing_classification: PricingClassification


class RouterSimulateRequest(BaseModel):
    task_requirements: dict


class RouterSimulateResponse(BaseModel):
    router_policy_version_id: Optional[str] = None
    eligible_candidates: list = []

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel

from app.db.enums import (
    CatalogRefreshStatus,
    HealthStatus,
    ModelStatus,
    PricingClassification,
    ProviderType,
    ToolCallingSupport,
)
from app.schemas.common import ORMModel, TimestampedRead


class ProviderCreate(BaseModel):
    type: ProviderType
    name: str
    base_url: Optional[str] = None
    # Write-only — never round-tripped back in ProviderRead. Stored via
    # SecretService (OS keychain), never as a column value.
    api_key: Optional[str] = None


class ProviderRead(TimestampedRead):
    type: ProviderType
    name: str
    base_url: Optional[str] = None
    health_status: HealthStatus


class ModelRead(TimestampedRead):
    canonical_model_id: str
    family: Optional[str] = None
    context_window: Optional[int] = None
    input_modalities: Optional[List[str]] = None
    output_modalities: Optional[List[str]] = None
    tool_calling_support: Optional[ToolCallingSupport] = None
    structured_output_support: Optional[bool] = None
    vision_capability: Optional[bool] = None
    reasoning_tier: Optional[str] = None
    coding_capability_tier: Optional[str] = None
    status: ModelStatus
    last_refreshed_at: Optional[datetime] = None


class ProviderModelRead(ORMModel):
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
    last_refreshed_at: datetime
    pricing_classification: PricingClassification


class ModelCatalogEntryRead(BaseModel):
    """One selectable (Model, Provider) offering — the granularity that
    actually matters for "what can I pick," since pricing/availability are
    provider-specific even when the canonical model is the same. Answers
    the Section 11 "model selection API" questions in one shape: which
    provider supplies it, current pricing + derived classification,
    capabilities (None where the provider doesn't reliably expose one —
    never guessed), and when the entry was last refreshed."""

    model_id: str
    canonical_model_id: str
    provider_id: str
    provider_model_id: str
    context_window: Optional[int] = None
    input_modalities: Optional[List[str]] = None
    output_modalities: Optional[List[str]] = None
    tool_calling_support: Optional[ToolCallingSupport] = None
    structured_output_support: Optional[bool] = None
    vision_capability: Optional[bool] = None
    cost_input_per_mtok: Optional[Decimal] = None
    cost_output_per_mtok: Optional[Decimal] = None
    pricing_classification: PricingClassification
    model_status: ModelStatus
    last_refreshed_at: datetime


class CatalogRefreshRead(ORMModel):
    id: str
    provider_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: CatalogRefreshStatus
    models_discovered: int
    models_added: int
    models_updated: int
    models_unavailable: int
    error: Optional[dict] = None


class ConnectionTestResult(BaseModel):
    """Section 13 connectivity test — never carries the API key in either
    direction."""

    status: HealthStatus
    detail: Optional[str] = None


class RouterSimulateRequest(BaseModel):
    task_requirements: dict


class RouterSimulateResponse(BaseModel):
    router_policy_version_id: Optional[str] = None
    eligible_candidates: list = []

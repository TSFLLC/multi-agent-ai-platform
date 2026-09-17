"""Model/Provider Registry resource boundary — Section 25.2, 13, 14.5, MA2.

Every endpoint requires authentication (app.auth.get_current_user);
provider/model administration additionally requires
app.authz.require_platform_admin — models/providers carry no project_id,
so this is platform-wide administration, never anonymous (Owner
instruction, Section 14).
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.auth import get_current_user
from app.authz import require_platform_admin
from app.bootstrap import LOCAL_PROJECT_ID
from app.db.enums import HealthStatus, PricingClassification, ToolCallingSupport
from app.errors import NotFoundError
from app.models.identity import User
from app.models.providers import Model, Provider, ProviderCatalogRefresh, ProviderModel
from app.providers.factory import build_provider_adapter
from app.schemas.providers import (
    CatalogRefreshRead,
    ConnectionTestResult,
    ModelCatalogEntryRead,
    ModelRead,
    ProviderCreate,
    ProviderModelRead,
    ProviderRead,
    RouterSimulateResponse,
)
from app.services.model_registry_service import ModelRegistryService
from app.services.secret_service import SecretService

router = APIRouter(tags=["models"])


@router.get("/models", response_model=List[ModelCatalogEntryRead])
def list_models(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
    pricing: Optional[PricingClassification] = Query(
        default=None, description="Filter by FREE/PAID/UNKNOWN pricing classification."
    ),
    tool_calling_support: Optional[ToolCallingSupport] = Query(default=None),
    min_context_window: Optional[int] = Query(default=None),
    provider_id: Optional[str] = Query(default=None),
):
    return ModelRegistryService(db).list_catalog(
        pricing=pricing,
        tool_calling_support=tool_calling_support,
        min_context_window=min_context_window,
        provider_id=provider_id,
    )


@router.get("/models/{model_id}", response_model=ModelRead)
def get_model(model_id: str, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    model = db.get(Model, model_id)
    if model is None:
        raise NotFoundError(f"Model {model_id} not found.")
    return model


@router.get("/models/{model_id}/provider-models", response_model=List[ProviderModelRead])
def list_provider_models(
    model_id: str, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
):
    stmt = select(ProviderModel).where(ProviderModel.model_id == model_id)
    return list(db.execute(stmt).scalars().all())


@router.post("/models/refresh", response_model=CatalogRefreshRead, status_code=202)
def refresh_models(
    provider_id: str = Query(...),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_platform_admin),
):
    """Section 13.3/7 — controlled catalog refresh, no restart required.
    A network/auth failure is recorded but never destroys the
    last-known-good catalog (ModelRegistryService.refresh_catalog)."""
    provider = db.get(Provider, provider_id)
    if provider is None:
        raise NotFoundError(f"Provider {provider_id} not found.")
    adapter = build_provider_adapter(db, provider)
    return ModelRegistryService(db).refresh_catalog(provider, adapter)


@router.get("/providers", response_model=List[ProviderRead])
def list_providers(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return list(db.execute(select(Provider)).scalars().all())


@router.post("/providers", response_model=ProviderRead, status_code=201)
def create_provider(
    body: ProviderCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_platform_admin),
):
    """The raw API key (if given) is written straight to the local secret
    store and never persisted in the ``providers`` row or echoed back —
    ``ProviderRead`` has no credential field at all."""
    provider = Provider(type=body.type, name=body.name, base_url=body.base_url)
    db.add(provider)
    db.flush()

    if body.api_key:
        SecretService(db).store_secret(
            project_id=LOCAL_PROJECT_ID,
            provider_id=provider.id,
            name=f"{body.type.value}_api_key",
            value=body.api_key,
            created_by=admin.id,
        )

    db.commit()
    db.refresh(provider)
    return provider


@router.post("/providers/{provider_id}/test-connection", response_model=ConnectionTestResult)
def test_provider_connection(
    provider_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_platform_admin),
):
    """Section 13 connectivity test — never prints/logs/returns the API
    key; distinguishes authentication failure from network/provider
    failure via the adapter's own health_check() result."""
    provider = db.get(Provider, provider_id)
    if provider is None:
        raise NotFoundError(f"Provider {provider_id} not found.")

    adapter = build_provider_adapter(db, provider)
    health = adapter.health_check()
    provider.health_status = HealthStatus(health.status)
    db.commit()
    return ConnectionTestResult(status=HealthStatus(health.status), detail=health.detail)


@router.get("/providers/{provider_id}/refreshes", response_model=List[CatalogRefreshRead])
def list_provider_refreshes(
    provider_id: str, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
):
    stmt = (
        select(ProviderCatalogRefresh)
        .where(ProviderCatalogRefresh.provider_id == provider_id)
        .order_by(ProviderCatalogRefresh.started_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


@router.get("/router/simulate", response_model=RouterSimulateResponse)
def simulate_router(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Section 25.5 dry-run — must return the same
    router_policy_version_id/eligible_candidates shape a real routing
    decision would record. No intelligent Router exists yet (MA8);
    contract-only in MA2, same as MA0/MA1."""
    not_implemented()

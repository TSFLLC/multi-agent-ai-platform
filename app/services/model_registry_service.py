"""Model Registry Service — Section 11, 13.3, MA2.

Talks only to the generic ``ProviderAdapter`` interface (Section 13.1) —
never imports or references ``OpenRouterAdapter`` by name, never
hard-codes provider-specific behavior (Section 13.5).

Refresh discipline: the adapter's ``list_models()`` network call happens
*before* any database transaction opens; all DB writes for one refresh
then happen without further network I/O, and commit once at the end. A
network/auth failure leaves the last-known-good catalog completely
untouched — only a ``provider_catalog_refreshes`` row records the
failure.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import CatalogRefreshStatus, ModelStatus, PricingClassification, ToolCallingSupport
from app.domain.pricing import classify_pricing
from app.models.providers import (
    Model,
    Provider,
    ProviderCatalogRefresh,
    ProviderModel,
    ProviderModelSnapshot,
)
from app.providers.base import (
    ModelDescriptor,
    ProviderAdapter,
    ProviderAuthenticationError,
    ProviderConnectionError,
)

_TOOL_CALLING_VALUES = {member.value for member in ToolCallingSupport}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _map_tool_calling(value: Optional[str]) -> Optional[ToolCallingSupport]:
    if value is None or value not in _TOOL_CALLING_VALUES:
        return None
    return ToolCallingSupport(value)


class ModelRegistryService:
    def __init__(self, db: Session):
        self.db = db

    def refresh_catalog(self, provider: Provider, adapter: ProviderAdapter) -> ProviderCatalogRefresh:
        started_at = _utcnow()
        try:
            descriptors = adapter.list_models()
        except (ProviderAuthenticationError, ProviderConnectionError) as exc:
            return self._record_failed_refresh(provider, started_at, exc)

        added = 0
        updated = 0
        seen_provider_model_ids = set()

        for descriptor in descriptors:
            model = self._upsert_model(descriptor)
            provider_model, was_created = self._upsert_provider_model(model, provider, descriptor)
            seen_provider_model_ids.add(provider_model.id)
            if was_created:
                added += 1
            else:
                updated += 1
            self._create_snapshot(provider_model, model, provider)
            if model.status != ModelStatus.ACTIVE:
                model.status = ModelStatus.ACTIVE  # rediscovered after a prior refresh missed it

        unavailable = self._mark_missing_unavailable(provider, seen_provider_model_ids)

        refresh = ProviderCatalogRefresh(
            provider_id=provider.id,
            started_at=started_at,
            completed_at=_utcnow(),
            status=CatalogRefreshStatus.SUCCESS,
            models_discovered=len(descriptors),
            models_added=added,
            models_updated=updated,
            models_unavailable=unavailable,
        )
        self.db.add(refresh)
        self.db.commit()
        self.db.refresh(refresh)
        return refresh

    def _record_failed_refresh(
        self, provider: Provider, started_at: datetime, exc: Exception
    ) -> ProviderCatalogRefresh:
        refresh = ProviderCatalogRefresh(
            provider_id=provider.id,
            started_at=started_at,
            completed_at=_utcnow(),
            status=CatalogRefreshStatus.FAILED,
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        self.db.add(refresh)
        self.db.commit()
        self.db.refresh(refresh)
        return refresh

    def _upsert_model(self, descriptor: ModelDescriptor) -> Model:
        # MA2 limitation, documented not hidden: canonical_model_id is set
        # to the provider's own model id. With exactly one provider
        # (OpenRouter) this is harmless; merging the same underlying model
        # exposed by two different providers into one canonical identity
        # is a real future problem this does not attempt to solve.
        model = self.db.execute(
            select(Model).where(Model.canonical_model_id == descriptor.provider_model_id)
        ).scalar_one_or_none()

        tool_calling = _map_tool_calling(descriptor.tool_calling_support)

        if model is None:
            model = Model(
                canonical_model_id=descriptor.provider_model_id,
                context_window=descriptor.context_window,
                input_modalities=descriptor.input_modalities,
                output_modalities=descriptor.output_modalities,
                tool_calling_support=tool_calling,
                structured_output_support=descriptor.structured_output_support,
                vision_capability=(
                    "image" in (descriptor.input_modalities or []) if descriptor.input_modalities else None
                ),
                status=ModelStatus.ACTIVE,
                last_refreshed_at=_utcnow(),
            )
            self.db.add(model)
            self.db.flush()
        else:
            model.context_window = descriptor.context_window
            model.input_modalities = descriptor.input_modalities
            model.output_modalities = descriptor.output_modalities
            if tool_calling is not None:
                model.tool_calling_support = tool_calling
            if descriptor.structured_output_support is not None:
                model.structured_output_support = descriptor.structured_output_support
            if descriptor.input_modalities is not None:
                model.vision_capability = "image" in descriptor.input_modalities
            model.last_refreshed_at = _utcnow()
        return model

    def _upsert_provider_model(
        self, model: Model, provider: Provider, descriptor: ModelDescriptor
    ) -> Tuple[ProviderModel, bool]:
        provider_model = self.db.execute(
            select(ProviderModel).where(
                ProviderModel.model_id == model.id, ProviderModel.provider_id == provider.id
            )
        ).scalar_one_or_none()

        was_created = provider_model is None
        if provider_model is None:
            provider_model = ProviderModel(
                model_id=model.id,
                provider_id=provider.id,
                provider_model_id=descriptor.provider_model_id,
                cost_input_per_mtok=descriptor.cost_input_per_mtok,
                cost_output_per_mtok=descriptor.cost_output_per_mtok,
                last_refreshed_at=_utcnow(),
            )
            self.db.add(provider_model)
            self.db.flush()
        else:
            provider_model.cost_input_per_mtok = descriptor.cost_input_per_mtok
            provider_model.cost_output_per_mtok = descriptor.cost_output_per_mtok
            provider_model.last_refreshed_at = _utcnow()

        return provider_model, was_created

    def _create_snapshot(self, provider_model: ProviderModel, model: Model, provider: Provider) -> None:
        capability_snapshot = {
            "tool_calling_support": (
                model.tool_calling_support.value if model.tool_calling_support else None
            ),
            "structured_output_support": model.structured_output_support,
            "vision_capability": model.vision_capability,
        }
        snapshot = ProviderModelSnapshot(
            provider_model_id=provider_model.id,
            model_id=model.id,
            provider_id=provider.id,
            pricing_input_per_mtok=provider_model.cost_input_per_mtok,
            pricing_output_per_mtok=provider_model.cost_output_per_mtok,
            context_window=model.context_window,
            capability_snapshot=capability_snapshot,
        )
        self.db.add(snapshot)

    def _mark_missing_unavailable(self, provider: Provider, seen_provider_model_ids: set) -> int:
        """A model that disappears from a provider's catalog is marked
        unavailable, not deleted (Section 13.3) — historical Agent Runs
        still resolve which model/pricing they used via their frozen
        provider_model_snapshot."""
        existing = (
            self.db.execute(select(ProviderModel).where(ProviderModel.provider_id == provider.id))
            .scalars()
            .all()
        )

        count = 0
        for provider_model in existing:
            if provider_model.id in seen_provider_model_ids:
                continue
            if provider_model.model.status != ModelStatus.UNAVAILABLE:
                provider_model.model.status = ModelStatus.UNAVAILABLE
                count += 1
        return count

    # -- Model selection API (Section 11) ------------------------------------

    def list_catalog(
        self,
        *,
        pricing: Optional[PricingClassification] = None,
        tool_calling_support: Optional[ToolCallingSupport] = None,
        min_context_window: Optional[int] = None,
        provider_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """One row per (Model, Provider) offering — the granularity that
        actually matters for selection, since pricing/availability are
        provider-specific. Filtering happens over normalized registry
        data (never OpenRouter-specific route logic, Section 11)."""
        stmt = select(ProviderModel, Model).join(Model, ProviderModel.model_id == Model.id)
        if provider_id is not None:
            stmt = stmt.where(ProviderModel.provider_id == provider_id)
        if tool_calling_support is not None:
            stmt = stmt.where(Model.tool_calling_support == tool_calling_support)
        if min_context_window is not None:
            stmt = stmt.where(Model.context_window >= min_context_window)

        entries = []
        for provider_model, model in self.db.execute(stmt).all():
            classification = classify_pricing(
                provider_model.cost_input_per_mtok, provider_model.cost_output_per_mtok
            )
            if pricing is not None and classification != pricing:
                continue
            entries.append(
                {
                    "id": provider_model.id,
                    "model_id": model.id,
                    "canonical_model_id": model.canonical_model_id,
                    "provider_id": provider_model.provider_id,
                    "provider_model_id": provider_model.provider_model_id,
                    "context_window": model.context_window,
                    "input_modalities": model.input_modalities,
                    "output_modalities": model.output_modalities,
                    "tool_calling_support": model.tool_calling_support,
                    "structured_output_support": model.structured_output_support,
                    "vision_capability": model.vision_capability,
                    "cost_input_per_mtok": provider_model.cost_input_per_mtok,
                    "cost_output_per_mtok": provider_model.cost_output_per_mtok,
                    "pricing_classification": classification,
                    "model_status": model.status,
                    "last_refreshed_at": provider_model.last_refreshed_at,
                }
            )
        return entries

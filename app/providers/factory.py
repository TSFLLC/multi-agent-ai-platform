"""The one place that maps ``providers.type`` to a concrete adapter
instance. Everything else (ModelRegistryService, the API routers) only
ever sees the generic ``ProviderAdapter`` interface — this factory is the
seam a new provider type is added at, never a change to the registry
service or Agent/Task/Evaluation contracts (Section 13.5).
"""

from sqlalchemy.orm import Session

from app.db.enums import ProviderType
from app.models.providers import Provider
from app.providers.base import ProviderAdapter
from app.providers.openrouter import DEFAULT_BASE_URL, OpenRouterAdapter
from app.services.secret_service import SecretService


def build_provider_adapter(db: Session, provider: Provider) -> ProviderAdapter:
    if provider.type == ProviderType.OPENROUTER:
        api_key = SecretService(db).get_current_provider_api_key(provider.id)
        base_url = provider.base_url or DEFAULT_BASE_URL
        return OpenRouterAdapter(api_key, base_url=base_url)

    raise NotImplementedError(
        f"No ProviderAdapter implementation exists yet for provider type {provider.type.value!r}."
    )

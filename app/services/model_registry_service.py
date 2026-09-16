"""Model Registry Service — Section 11. Provider Adapter orchestration,
capability/cost refresh, staleness tracking. Real implementation: MA2.

Must talk only to the ``ProviderAdapter`` interface (Section 13.1), never
hard-code OpenRouter into this service's logic (Section 13.5).
"""

from app.services.base import BaseService


class ModelRegistryService(BaseService):
    def refresh_catalog(self, provider_id: str):
        raise NotImplementedError("ModelRegistryService lands in MA2")

    def snapshot_provider_model(self, provider_model_id: str):
        """Writes a provider_model_snapshots row (Section 24.4 #8) — the
        immutable pricing/capability context an Agent Run binds to."""
        raise NotImplementedError("ModelRegistryService lands in MA2")

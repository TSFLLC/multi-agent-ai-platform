"""Model Registry Service — catalog refresh, FREE/PAID/UNKNOWN, historical
snapshots — Section 11, 13.3, MA2.

Uses a FakeAdapter (not OpenRouterAdapter) so these tests exercise the
registry's own logic against the generic ProviderAdapter interface —
never the internet, never OpenRouter-specific code.
"""

from decimal import Decimal
from typing import List, Optional

from app.db.enums import CatalogRefreshStatus, ModelStatus, PricingClassification
from app.models.providers import Model, ProviderCatalogRefresh, ProviderModel, ProviderModelSnapshot
from app.providers.base import (
    ModelDescriptor,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderHealth,
)
from app.services.model_registry_service import ModelRegistryService
from tests.conftest import make_provider


class FakeAdapter:
    def __init__(self, models: Optional[List[ModelDescriptor]] = None, error: Optional[Exception] = None):
        self._models = models or []
        self._error = error

    def list_models(self) -> List[ModelDescriptor]:
        if self._error:
            raise self._error
        return self._models

    def get_model(self, provider_model_id):
        for m in self._models:
            if m.provider_model_id == provider_model_id:
                return m
        return None

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(status="up")

    def invoke(self, *args, **kwargs):
        raise NotImplementedError


def _descriptor(**overrides) -> ModelDescriptor:
    defaults = {
        "provider_model_id": "test/model-a",
        "name": "Model A",
        "context_window": 8192,
        "cost_input_per_mtok": Decimal("1.00"),
        "cost_output_per_mtok": Decimal("2.00"),
    }
    defaults.update(overrides)
    return ModelDescriptor(**defaults)


def test_refresh_adds_new_models(db):
    provider = make_provider(db)
    adapter = FakeAdapter([_descriptor(), _descriptor(provider_model_id="test/model-b")])

    result = ModelRegistryService(db).refresh_catalog(provider, adapter)

    assert result.status == CatalogRefreshStatus.SUCCESS
    assert result.models_discovered == 2
    assert result.models_added == 2
    assert result.models_updated == 0
    assert db.query(Model).count() == 2
    assert db.query(ProviderModel).count() == 2


def test_refresh_updates_existing_models(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("1.00"))]))

    result = svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("5.00"))]))

    assert result.models_added == 0
    assert result.models_updated == 1
    provider_model = db.query(ProviderModel).one()
    assert provider_model.cost_input_per_mtok == Decimal("5.00")


def test_refresh_is_idempotent_no_duplicate_rows(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    descriptor = _descriptor()

    svc.refresh_catalog(provider, FakeAdapter([descriptor]))
    svc.refresh_catalog(provider, FakeAdapter([descriptor]))
    svc.refresh_catalog(provider, FakeAdapter([descriptor]))

    assert db.query(Model).count() == 1
    assert db.query(ProviderModel).count() == 1


def test_missing_model_marked_unavailable_not_deleted(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(provider_model_id="test/gone")]))

    result = svc.refresh_catalog(provider, FakeAdapter([_descriptor(provider_model_id="test/new")]))

    assert result.models_unavailable == 1
    assert db.query(Model).count() == 2  # both still present
    gone = db.query(Model).filter_by(canonical_model_id="test/gone").one()
    assert gone.status == ModelStatus.UNAVAILABLE


def test_model_rediscovered_after_absence_becomes_active_again(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    d = _descriptor(provider_model_id="test/flaky")

    svc.refresh_catalog(provider, FakeAdapter([d]))
    svc.refresh_catalog(provider, FakeAdapter([]))  # disappears
    model = db.query(Model).filter_by(canonical_model_id="test/flaky").one()
    assert model.status == ModelStatus.UNAVAILABLE

    svc.refresh_catalog(provider, FakeAdapter([d]))  # reappears
    db.refresh(model)
    assert model.status == ModelStatus.ACTIVE


def test_refresh_failure_preserves_last_known_good_catalog(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor()]))
    good_provider_model = db.query(ProviderModel).one()
    good_cost = good_provider_model.cost_input_per_mtok

    result = svc.refresh_catalog(provider, FakeAdapter(error=ProviderConnectionError("network down")))

    assert result.status == CatalogRefreshStatus.FAILED
    assert result.error["type"] == "ProviderConnectionError"
    db.refresh(good_provider_model)
    assert good_provider_model.cost_input_per_mtok == good_cost  # untouched
    assert db.query(Model).count() == 1  # nothing deleted or corrupted


def test_refresh_authentication_failure_recorded(db):
    provider = make_provider(db)
    result = ModelRegistryService(db).refresh_catalog(
        provider, FakeAdapter(error=ProviderAuthenticationError("bad key"))
    )
    assert result.status == CatalogRefreshStatus.FAILED
    assert result.error["type"] == "ProviderAuthenticationError"


def test_refresh_records_are_never_deleted_across_multiple_refreshes(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor()]))
    svc.refresh_catalog(provider, FakeAdapter([_descriptor()]))
    assert db.query(ProviderCatalogRefresh).filter_by(provider_id=provider.id).count() == 2


# --- historical snapshots ---------------------------------------------------


def test_snapshot_created_on_every_refresh(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("1.00"))]))
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("2.00"))]))

    snapshots = db.query(ProviderModelSnapshot).order_by(ProviderModelSnapshot.snapshotted_at).all()
    assert len(snapshots) == 2
    assert snapshots[0].pricing_input_per_mtok == Decimal("1.00")
    assert snapshots[1].pricing_input_per_mtok == Decimal("2.00")


def test_price_change_never_rewrites_older_snapshot(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("1.00"))]))
    first_snapshot = db.query(ProviderModelSnapshot).one()
    first_snapshot_id = first_snapshot.id
    first_price = first_snapshot.pricing_input_per_mtok

    svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("99.00"))]))

    db.refresh(first_snapshot)
    assert first_snapshot.id == first_snapshot_id
    assert first_snapshot.pricing_input_per_mtok == first_price  # unchanged


# --- FREE/PAID/UNKNOWN via list_catalog -------------------------------------


def test_list_catalog_pricing_filter_free(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(
        provider,
        FakeAdapter(
            [
                _descriptor(
                    provider_model_id="test/free",
                    cost_input_per_mtok=Decimal(0),
                    cost_output_per_mtok=Decimal(0),
                ),
                _descriptor(
                    provider_model_id="test/paid",
                    cost_input_per_mtok=Decimal(1),
                    cost_output_per_mtok=Decimal(2),
                ),
                _descriptor(
                    provider_model_id="test/unknown", cost_input_per_mtok=None, cost_output_per_mtok=None
                ),
            ]
        ),
    )

    free = svc.list_catalog(pricing=PricingClassification.FREE)
    paid = svc.list_catalog(pricing=PricingClassification.PAID)
    unknown = svc.list_catalog(pricing=PricingClassification.UNKNOWN)
    everything = svc.list_catalog()

    assert [e["provider_model_id"] for e in free] == ["test/free"]
    assert [e["provider_model_id"] for e in paid] == ["test/paid"]
    assert [e["provider_model_id"] for e in unknown] == ["test/unknown"]
    assert len(everything) == 3


def test_list_catalog_never_hard_codes_free_model_ids(db):
    """No FREE_MODELS-style static list anywhere in the service module."""
    import inspect

    from app import services

    source = inspect.getsource(services.model_registry_service)
    assert "FREE_MODELS" not in source
    assert ":free" not in source  # no OpenRouter free-suffix convention baked in here


def test_list_catalog_filters_by_min_context_window(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(
        provider,
        FakeAdapter(
            [
                _descriptor(provider_model_id="test/small", context_window=4096),
                _descriptor(provider_model_id="test/large", context_window=128000),
            ]
        ),
    )

    results = svc.list_catalog(min_context_window=100000)
    assert [e["provider_model_id"] for e in results] == ["test/large"]


def test_capability_unknown_is_none_not_guessed_false(db):
    """Section 3: a model whose adapter payload never reliably indicated
    tool-calling support must store None, not a guessed False/NONE."""
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(
        provider, FakeAdapter([_descriptor(tool_calling_support=None, structured_output_support=None)])
    )
    model = db.query(Model).one()
    assert model.tool_calling_support is None
    assert model.structured_output_support is None

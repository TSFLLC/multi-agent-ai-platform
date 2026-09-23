"""AIL.1B — catalog-refresh change detection for provider_model_snapshots.

Verifies the diff gate added to ModelRegistryService: a catalog-refresh
snapshot is written only when something genuinely differs from the previous
catalog-refresh snapshot for that offering (never on a no-op refresh, never
backfilled/guessed), tagged with every dimension that changed, and kept
fully separate from app.model_resolution.freeze_snapshot's execution-time
snapshots (a distinct, untouched write path).
"""

from decimal import Decimal

from app.db.enums import ModelStatus, SnapshotSource
from app.model_resolution import freeze_snapshot
from app.models.providers import Model, ProviderModelSnapshot
from tests.conftest import make_provider
from tests.test_model_registry_service import FakeAdapter, _descriptor
from app.services.model_registry_service import ModelRegistryService


def _catalog_snapshots(db, provider_model_id=None):
    q = db.query(ProviderModelSnapshot).filter(
        ProviderModelSnapshot.source == SnapshotSource.CATALOG_REFRESH
    )
    if provider_model_id:
        q = q.filter(ProviderModelSnapshot.provider_model_id == provider_model_id)
    return q.order_by(ProviderModelSnapshot.snapshotted_at).all()


def test_first_sighting_is_tagged_new(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor()]))

    snapshots = _catalog_snapshots(db)
    assert len(snapshots) == 1
    assert snapshots[0].change_kinds == ["new"]
    assert snapshots[0].source == SnapshotSource.CATALOG_REFRESH


def test_noop_refresh_creates_no_fake_history_event(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    descriptor = _descriptor()
    svc.refresh_catalog(provider, FakeAdapter([descriptor]))
    svc.refresh_catalog(provider, FakeAdapter([descriptor]))  # identical — no-op
    svc.refresh_catalog(provider, FakeAdapter([descriptor]))  # identical — no-op

    snapshots = _catalog_snapshots(db)
    assert len(snapshots) == 1  # only the first (new) sighting
    assert snapshots[0].change_kinds == ["new"]


def test_price_change_recorded(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("1.00"))]))
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("5.00"))]))

    snapshots = _catalog_snapshots(db)
    assert len(snapshots) == 2
    assert snapshots[1].change_kinds == ["price"]
    assert snapshots[1].pricing_input_per_mtok == Decimal("5.00")


def test_context_change_recorded(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(context_window=8192)]))
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(context_window=32768)]))

    snapshots = _catalog_snapshots(db)
    assert snapshots[1].change_kinds == ["context"]
    assert snapshots[1].context_window == 32768


def test_capability_change_recorded(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(structured_output_support=False)]))
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(structured_output_support=True)]))

    snapshots = _catalog_snapshots(db)
    assert snapshots[1].change_kinds == ["capability"]
    assert snapshots[1].capability_snapshot["structured_output_support"] is True


def test_status_change_recorded_on_disappearance_and_rediscovery(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    d = _descriptor(provider_model_id="test/flaky")

    svc.refresh_catalog(provider, FakeAdapter([d]))
    svc.refresh_catalog(provider, FakeAdapter([]))  # disappears -> unavailable

    model = db.query(Model).filter_by(canonical_model_id="test/flaky").one()
    snapshots = _catalog_snapshots(db)
    assert model.status == ModelStatus.UNAVAILABLE
    assert snapshots[-1].change_kinds == ["status"]
    assert snapshots[-1].capability_snapshot["model_status"] == "unavailable"

    svc.refresh_catalog(provider, FakeAdapter([d]))  # reappears -> active again
    snapshots = _catalog_snapshots(db)
    assert snapshots[-1].change_kinds == ["status"]
    assert snapshots[-1].capability_snapshot["model_status"] == "active"


def test_multiple_simultaneous_dimensions_are_all_recorded_deterministically(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(
        provider,
        FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("1.00"), context_window=8192)]),
    )
    svc.refresh_catalog(
        provider,
        FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("2.00"), context_window=32768)]),
    )

    snapshots = _catalog_snapshots(db)
    assert set(snapshots[1].change_kinds) == {"price", "context"}
    # Deterministic ordering — not an opaque combined score of any kind.
    assert snapshots[1].change_kinds == ["price", "context"]


def test_null_pricing_transition_is_a_real_change_never_carried_forward(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("1.00"))]))
    svc.refresh_catalog(
        provider,
        FakeAdapter([_descriptor(cost_input_per_mtok=None, cost_output_per_mtok=None)]),
    )

    snapshots = _catalog_snapshots(db)
    assert snapshots[1].change_kinds == ["price"]
    assert snapshots[1].pricing_input_per_mtok is None  # never carries the old $1.00 forward


def test_old_snapshots_are_never_rewritten_or_reclassified(db):
    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("1.00"))]))
    first = _catalog_snapshots(db)[0]
    first_id, first_change_kinds = first.id, list(first.change_kinds)

    svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("2.00"))]))
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(cost_input_per_mtok=Decimal("3.00"))]))

    db.refresh(first)
    assert first.id == first_id
    assert first.change_kinds == first_change_kinds  # untouched by later refreshes


def test_execution_time_freeze_snapshot_is_unaffected_and_never_enters_change_history(db):
    """app.model_resolution.freeze_snapshot keeps writing a fresh row every
    time, exactly as before AIL.1B — it must never be diff-gated and must
    never show up in the catalog change-history feed."""
    from app.db.enums import PricingClassification
    from app.model_resolution import ResolvedModel
    from tests.conftest import make_model, make_provider_model

    provider = make_provider(db)
    model = make_model(db)
    pm = make_provider_model(db, model=model, provider=provider)

    resolved = ResolvedModel(
        provider_model=pm,
        model=model,
        provider=provider,
        pricing_classification=PricingClassification.PAID,
        rationale="test",
        eligible_candidate_ids=[pm.id],
    )
    snap1 = freeze_snapshot(db, resolved)
    snap2 = freeze_snapshot(db, resolved)
    db.commit()

    assert snap1.id != snap2.id  # a fresh row every time, never reused
    assert snap1.source == SnapshotSource.EXECUTION_FREEZE
    assert snap2.source == SnapshotSource.EXECUTION_FREEZE
    assert snap1.change_kinds is None
    assert snap2.change_kinds is None
    assert _catalog_snapshots(db, provider_model_id=pm.id) == []  # no catalog history noise

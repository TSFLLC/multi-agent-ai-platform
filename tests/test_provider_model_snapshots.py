"""Provider/Model snapshot reproducibility — Section 24.4 #8, Acceptance
Criterion 11: a historical Agent Run's recorded pricing/capability data
must always reflect what applied *at that run's execution time*, even
after the live registry row is later refreshed to different values."""

from decimal import Decimal

from app.db.enums import ProviderType
from app.models.providers import Model, Provider, ProviderModel, ProviderModelSnapshot


def _make_model_provider(db):
    model = Model(canonical_model_id="deepseek/deepseek-chat")
    provider = Provider(type=ProviderType.OPENROUTER, name="OpenRouter")
    db.add_all([model, provider])
    db.commit()
    pm = ProviderModel(
        model_id=model.id,
        provider_id=provider.id,
        provider_model_id="deepseek/deepseek-chat",
        cost_input_per_mtok=Decimal("0.14"),
        cost_output_per_mtok=Decimal("0.28"),
    )
    db.add(pm)
    db.commit()
    return model, provider, pm


def test_snapshot_preserves_pricing_after_live_row_changes(db):
    model, provider, pm = _make_model_provider(db)

    snapshot = ProviderModelSnapshot(
        provider_model_id=pm.id,
        model_id=model.id,
        provider_id=provider.id,
        pricing_input_per_mtok=pm.cost_input_per_mtok,
        pricing_output_per_mtok=pm.cost_output_per_mtok,
        context_window=64000,
    )
    db.add(snapshot)
    db.commit()

    # A catalog refresh (Section 13.3) mutates the live row in place.
    pm.cost_input_per_mtok = Decimal("0.50")
    pm.cost_output_per_mtok = Decimal("1.00")
    db.commit()

    db.refresh(snapshot)
    assert snapshot.pricing_input_per_mtok == Decimal("0.14")
    assert snapshot.pricing_output_per_mtok == Decimal("0.28")
    assert pm.cost_input_per_mtok == Decimal("0.50")


def test_snapshot_captures_capability_context(db):
    model, provider, pm = _make_model_provider(db)
    snapshot = ProviderModelSnapshot(
        provider_model_id=pm.id,
        model_id=model.id,
        provider_id=provider.id,
        capability_snapshot={"tool_calling_support": "structured", "reasoning_tier": "high"},
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    assert snapshot.capability_snapshot["reasoning_tier"] == "high"


def test_two_snapshots_of_the_same_provider_model_can_diverge_over_time(db):
    """Two Agent Runs six months apart against "the same model_id" must be
    able to show different pricing, each accurate for its own run."""
    model, provider, pm = _make_model_provider(db)

    snap1 = ProviderModelSnapshot(
        provider_model_id=pm.id,
        model_id=model.id,
        provider_id=provider.id,
        pricing_input_per_mtok=Decimal("0.14"),
    )
    db.add(snap1)
    db.commit()

    pm.cost_input_per_mtok = Decimal("0.09")
    db.commit()

    snap2 = ProviderModelSnapshot(
        provider_model_id=pm.id,
        model_id=model.id,
        provider_id=provider.id,
        pricing_input_per_mtok=pm.cost_input_per_mtok,
    )
    db.add(snap2)
    db.commit()

    assert snap1.pricing_input_per_mtok == Decimal("0.14")
    assert snap2.pricing_input_per_mtok == Decimal("0.09")

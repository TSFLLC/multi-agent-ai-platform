"""FREE/PAID/UNKNOWN pricing classification — Owner requirement, Section 3.

Classification must be derived from current provider pricing metadata,
never a hard-coded list of free model IDs.
"""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import PricingClassification, ProviderType
from app.domain.pricing import classify_pricing
from app.models.providers import Model, Provider, ProviderModel, ProviderModelSnapshot


def test_both_zero_is_free():
    assert classify_pricing(Decimal(0), Decimal(0)) == PricingClassification.FREE


def test_either_positive_is_paid():
    assert classify_pricing(Decimal("0.14"), Decimal("0.28")) == PricingClassification.PAID
    assert classify_pricing(Decimal(0), Decimal("0.28")) == PricingClassification.PAID
    assert classify_pricing(Decimal("0.14"), Decimal(0)) == PricingClassification.PAID


def test_missing_pricing_is_unknown_never_guessed():
    assert classify_pricing(None, None) == PricingClassification.UNKNOWN
    assert classify_pricing(None, Decimal("0.28")) == PricingClassification.UNKNOWN
    assert classify_pricing(Decimal("0.14"), None) == PricingClassification.UNKNOWN


def test_provider_model_exposes_pricing_classification(db):
    model = Model(canonical_model_id="meta-llama/llama-3-free")
    provider = Provider(type=ProviderType.OPENROUTER, name="OpenRouter")
    db.add_all([model, provider])
    db.commit()

    free_pm = ProviderModel(
        model_id=model.id,
        provider_id=provider.id,
        provider_model_id="meta-llama/llama-3-free",
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    db.add(free_pm)
    db.commit()

    assert free_pm.pricing_classification == PricingClassification.FREE


def test_provider_model_with_no_refresh_yet_is_unknown(db):
    model = Model(canonical_model_id="anthropic/claude-x")
    provider = Provider(type=ProviderType.OPENROUTER, name="OpenRouter")
    db.add_all([model, provider])
    db.commit()

    pm = ProviderModel(model_id=model.id, provider_id=provider.id, provider_model_id="anthropic/claude-x")
    db.add(pm)
    db.commit()

    assert pm.pricing_classification == PricingClassification.UNKNOWN


def test_snapshot_also_exposes_pricing_classification(db):
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

    snapshot = ProviderModelSnapshot(
        provider_model_id=pm.id,
        model_id=model.id,
        provider_id=provider.id,
        pricing_input_per_mtok=pm.cost_input_per_mtok,
        pricing_output_per_mtok=pm.cost_output_per_mtok,
    )
    db.add(snapshot)
    db.commit()

    assert snapshot.pricing_classification == PricingClassification.PAID


def test_negative_cost_never_misclassified_as_free_or_paid():
    """A negative value should never happen (CHECK constraint blocks it at
    the DB layer), but the pure classifier must not silently call it FREE
    or PAID if it ever did."""
    assert classify_pricing(Decimal(-1), Decimal(0)) == PricingClassification.UNKNOWN


def test_db_check_constraint_rejects_negative_cost(db):
    model = Model(canonical_model_id="x/y")
    provider = Provider(type=ProviderType.OPENROUTER, name="OpenRouter")
    db.add_all([model, provider])
    db.commit()

    db.add(
        ProviderModel(
            model_id=model.id,
            provider_id=provider.id,
            provider_model_id="x/y",
            cost_input_per_mtok=Decimal(-1),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

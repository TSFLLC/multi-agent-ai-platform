"""Model resolution — Section 14, MA3.

Manual resolution (the primary MA3 path), deterministic Auto policy
(FREE_ONLY/PREFER_FREE/ANY), and immutable snapshot freezing.
"""

from decimal import Decimal

import pytest

from app.db.enums import HealthStatus, ModelStatus, PricingClassification, RouterFreePolicy
from app.model_resolution import (
    ModelUnavailableError,
    NoEligibleModelError,
    freeze_snapshot,
    resolve_auto,
    resolve_manual,
)
from tests.conftest import make_model, make_provider, make_provider_model


def _free(db, canonical_model_id="free/model"):
    model = make_model(db, canonical_model_id=canonical_model_id)
    provider = make_provider(db)
    return make_provider_model(
        db,
        model=model,
        provider=provider,
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )


def _paid(db, canonical_model_id="paid/model", cost_in="3.00", cost_out="6.00"):
    model = make_model(db, canonical_model_id=canonical_model_id)
    provider = make_provider(db)
    return make_provider_model(
        db,
        model=model,
        provider=provider,
        cost_input_per_mtok=Decimal(cost_in),
        cost_output_per_mtok=Decimal(cost_out),
    )


def _unknown(db, canonical_model_id="unknown/model"):
    model = make_model(db, canonical_model_id=canonical_model_id)
    provider = make_provider(db)
    return make_provider_model(
        db, model=model, provider=provider, cost_input_per_mtok=None, cost_output_per_mtok=None
    )


# -- manual --------------------------------------------------------------


def test_resolve_manual_success(db):
    pm = _paid(db)
    db.commit()
    resolved = resolve_manual(db, pm.id)
    assert resolved.provider_model.id == pm.id
    assert resolved.pricing_classification == PricingClassification.PAID


def test_resolve_manual_missing_provider_model_raises(db):
    with pytest.raises(ModelUnavailableError):
        resolve_manual(db, "does-not-exist")


def test_resolve_manual_inactive_model_raises(db):
    model = make_model(db, canonical_model_id="deprecated/model", status=ModelStatus.DEPRECATED)
    pm = make_provider_model(db, model=model)
    db.commit()
    with pytest.raises(ModelUnavailableError):
        resolve_manual(db, pm.id)


def test_resolve_manual_down_provider_raises(db):
    provider = make_provider(db)
    provider.health_status = HealthStatus.DOWN
    pm = make_provider_model(db, provider=provider)
    db.commit()
    with pytest.raises(ModelUnavailableError):
        resolve_manual(db, pm.id)


# -- auto ------------------------------------------------------------------


def test_resolve_auto_free_only_selects_free(db):
    _paid(db)
    free_pm = _free(db)
    db.commit()
    resolved = resolve_auto(db, RouterFreePolicy.FREE_ONLY)
    assert resolved.provider_model.id == free_pm.id
    assert resolved.pricing_classification == PricingClassification.FREE


def test_resolve_auto_free_only_fails_clearly_with_no_free_model(db):
    """Never silently falls back to paid."""
    _paid(db)
    db.commit()
    with pytest.raises(NoEligibleModelError):
        resolve_auto(db, RouterFreePolicy.FREE_ONLY)


def test_resolve_auto_prefer_free_picks_free_over_paid(db):
    paid_pm = _paid(db)
    free_pm = _free(db)
    db.commit()
    resolved = resolve_auto(db, RouterFreePolicy.PREFER_FREE)
    assert resolved.provider_model.id == free_pm.id
    assert resolved.provider_model.id != paid_pm.id


def test_resolve_auto_prefer_free_falls_back_to_paid_when_no_free(db):
    paid_pm = _paid(db)
    db.commit()
    resolved = resolve_auto(db, RouterFreePolicy.PREFER_FREE)
    assert resolved.provider_model.id == paid_pm.id
    assert resolved.pricing_classification == PricingClassification.PAID


def test_resolve_auto_prefer_free_never_selects_unknown(db):
    """UNKNOWN pricing is excluded from PREFER_FREE eligibility entirely —
    an automatic policy cannot safely reason about a price it doesn't
    have, and UNKNOWN must never be silently treated as FREE."""
    _unknown(db)
    db.commit()
    with pytest.raises(NoEligibleModelError):
        resolve_auto(db, RouterFreePolicy.PREFER_FREE)


def test_resolve_auto_any_may_select_unknown_as_last_resort(db):
    unknown_pm = _unknown(db)
    db.commit()
    resolved = resolve_auto(db, RouterFreePolicy.ANY)
    assert resolved.provider_model.id == unknown_pm.id
    assert resolved.pricing_classification == PricingClassification.UNKNOWN


def test_resolve_auto_any_prefers_free_then_paid_then_unknown(db):
    unknown_pm = _unknown(db)
    paid_pm = _paid(db)
    free_pm = _free(db)
    db.commit()
    resolved = resolve_auto(db, RouterFreePolicy.ANY)
    assert resolved.provider_model.id == free_pm.id
    assert resolved.eligible_candidate_ids[0] == free_pm.id
    assert paid_pm.id in resolved.eligible_candidate_ids
    assert unknown_pm.id in resolved.eligible_candidate_ids


def test_resolve_auto_tie_break_is_deterministic_cheapest_then_alphabetical(db):
    cheap = _paid(db, canonical_model_id="aaa/cheap", cost_in="1.00", cost_out="1.00")
    expensive = _paid(db, canonical_model_id="zzz/expensive", cost_in="9.00", cost_out="9.00")
    db.commit()
    resolved = resolve_auto(db, RouterFreePolicy.ANY)
    assert resolved.provider_model.id == cheap.id
    assert resolved.provider_model.id != expensive.id


def test_resolve_auto_no_candidates_at_all_raises(db):
    with pytest.raises(NoEligibleModelError):
        resolve_auto(db, RouterFreePolicy.ANY)


# -- snapshot ----------------------------------------------------------------


def test_freeze_snapshot_captures_pricing_at_resolution_time(db):
    pm = _paid(db)
    db.commit()
    resolved = resolve_manual(db, pm.id)
    snapshot = freeze_snapshot(db, resolved)
    db.commit()

    assert snapshot.pricing_input_per_mtok == pm.cost_input_per_mtok
    assert snapshot.pricing_output_per_mtok == pm.cost_output_per_mtok

    # Mutating the live registry row afterward must never change the
    # already-frozen snapshot (Section 24.4 #8).
    pm.cost_input_per_mtok = Decimal("999.00")
    db.commit()
    db.refresh(snapshot)
    assert snapshot.pricing_input_per_mtok != Decimal("999.00")

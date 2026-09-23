from datetime import datetime, timedelta, timezone

import pytest

from app.db.enums import ModelStatus
from app.models.providers import Model, Provider, ProviderModel
from app.db.enums import HealthStatus, ProviderType
from app.models.radar import (
    ClaimCreationMethod,
    ClaimType,
    Development,
    DevelopmentStatus,
    RadarSource,
    RadarSourceClass,
    RadarSourceState,
)
from app.services.radar_service import (
    Freshness,
    RadarClaimService,
    RadarDevelopmentService,
    RadarItemService,
    RadarSourceService,
    RadarValidationError,
    content_hash,
    derive_freshness,
    derive_verification,
)


def make_source(db, source_class=RadarSourceClass.S2, state=RadarSourceState.ACTIVE):
    now = datetime.now(timezone.utc)
    source = RadarSource(
        name=f"source-{source_class.value}-{id(db)}-{id(source_class)}",
        source_class=source_class,
        endpoint_url="https://example.test/feed",
        independence_group="example",
        fetch_method="manual",
        cadence_minutes=60,
        state=state,
        owner_reviewed_at=now if state == RadarSourceState.ACTIVE else None,
        tos_reviewed_at=now if state == RadarSourceState.ACTIVE else None,
    )
    db.add(source)
    db.flush()
    return source


def make_development(db, key="release-1"):
    development = Development(title="Neutral release", development_type="model_release", candidate_key=key)
    db.add(development)
    db.flush()
    return development


def test_source_activation_requires_both_reviews(db):
    source = make_source(db, state=RadarSourceState.CANDIDATE)
    with pytest.raises(RadarValidationError):
        RadarSourceService(db).activate(source)


def test_source_item_hash_is_normalized_and_idempotent(db):
    source = make_source(db)
    service = RadarItemService(db)
    first = service.create_item(source=source, title="Release", normalized_content="hello\r\n world")
    second = service.create_item(source=source, title="Duplicate", normalized_content="hello world")
    assert first.id == second.id
    assert first.content_hash == content_hash("hello world")
    assert service.validate_quote(first, "hello") is None
    with pytest.raises(RadarValidationError):
        service.validate_quote(first, "not in source")


def test_inactive_source_cannot_receive_items(db):
    source = make_source(db, state=RadarSourceState.CANDIDATE)
    with pytest.raises(RadarValidationError):
        RadarItemService(db).create_item(source=source, title="Release", normalized_content="body")


def test_candidate_key_is_deterministic_and_reuses_development(db):
    service = RadarDevelopmentService(db)
    effective = datetime(2026, 9, 22, tzinfo=timezone.utc)
    first = service.get_or_create_candidate(
        title="First", development_type="model_release", subject_key="model:x", effective_at=effective, change_key="v1"
    )
    second = service.get_or_create_candidate(
        title="Different title", development_type="model_release", subject_key="model:x", effective_at=effective, change_key="v1"
    )
    assert first.id == second.id


def test_manual_merge_preserves_source_and_rejects_self_merge(db):
    source = make_development(db, "source")
    target = make_development(db, "target")
    RadarClaimService(db).merge_developments(source, target)
    assert source.status == DevelopmentStatus.MERGED
    assert source.merged_into_id == target.id
    with pytest.raises(RadarValidationError):
        RadarClaimService(db).merge_developments(target, target)


def test_claim_source_class_ceilings_and_quote_provenance(db):
    source = make_source(db, RadarSourceClass.S5)
    item = RadarItemService(db).create_item(source=source, title="Signal", normalized_content="users report adoption")
    development = make_development(db)
    claim = RadarClaimService(db).create_claim(
        claim_type=ClaimType.COMMUNITY_SIGNAL,
        text="Users report adoption",
        as_of=datetime.now(timezone.utc),
        created_by=ClaimCreationMethod.RULE,
        development_id=development.id,
        source_item_id=item.id,
        quote_span="users report adoption",
    )
    assert claim.id
    with pytest.raises(RadarValidationError):
        RadarClaimService(db).create_claim(
            claim_type=ClaimType.FACT,
            text="This is a fact",
            as_of=datetime.now(timezone.utc),
            created_by=ClaimCreationMethod.RULE,
            development_id=development.id,
            source_item_id=item.id,
        )


def test_claim_requires_exactly_one_origin_and_ai_citations(db):
    source = make_source(db)
    item = RadarItemService(db).create_item(source=source, title="Docs", normalized_content="documented API")
    development = make_development(db)
    with pytest.raises(RadarValidationError):
        RadarClaimService(db).create_claim(
            claim_type=ClaimType.FACT,
            text="missing origin",
            as_of=datetime.now(timezone.utc),
            created_by=ClaimCreationMethod.RULE,
            development_id=development.id,
        )
    evidence = RadarClaimService(db).create_claim(
        claim_type=ClaimType.FACT,
        text="documented API",
        as_of=datetime.now(timezone.utc),
        created_by=ClaimCreationMethod.RULE,
        development_id=development.id,
        source_item_id=item.id,
        quote_span="documented API",
    )
    with pytest.raises(RadarValidationError):
        RadarClaimService(db).create_claim(
            claim_type=ClaimType.AI_EXPLANATION,
            text="unsupported explanation",
            as_of=datetime.now(timezone.utc),
            created_by=ClaimCreationMethod.AGENT,
            development_id=development.id,
            source_item_id=item.id,
        )
    assert evidence.id


def test_canonical_model_link_and_verification_derivation(db):
    model = Model(canonical_model_id="canonical:test", status=ModelStatus.ACTIVE)
    provider = Provider(type=ProviderType.DIRECT, name="Test Provider", health_status=HealthStatus.UP)
    db.add_all([model, provider])
    development = make_development(db)
    from app.models.radar import DevelopmentModel

    db.add(DevelopmentModel(development_id=development.id, model_id=model.id))
    db.add(ProviderModel(model_id=model.id, provider_id=provider.id, provider_model_id="canonical:test"))
    db.flush()
    assert derive_verification(db, development.id) == "Available"


def test_freshness_is_separate_from_verification(db):
    source = make_source(db)
    source.last_success_at = datetime.now(timezone.utc) - timedelta(hours=4)
    assert derive_freshness(source) == Freshness.STALE
    source.last_success_at = datetime.now(timezone.utc)
    assert derive_freshness(source) == Freshness.CURRENT

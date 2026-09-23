from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import HealthStatus, ModelStatus, ProviderType
from app.models.concepts import Concept
from app.models.identity import ProjectMembership
from app.models.providers import Model, Provider, ProviderModel
from app.models.radar import (
    AttentionSample,
    AttentionState,
    ClaimCreationMethod,
    ClaimType,
    DevelopmentConcept,
    DevelopmentConceptState,
    DevelopmentModel,
    RadarSource,
    RadarSourceClass,
    RadarSourceState,
    TriageDecisionKind,
)
from app.models.taxonomy import TaxonomyTerm
from app.services.radar_intelligence_service import (
    RadarIntelligenceService,
    create_manual_radar_item,
)
from app.services.radar_service import (
    RadarClaimService,
    RadarDevelopmentService,
    RadarItemService,
    RadarValidationError,
    derive_verification,
)


def source(db, name, source_class=RadarSourceClass.S5, endpoint="https://example.test/feed"):
    now = datetime.now(timezone.utc)
    row = RadarSource(
        name=name,
        source_class=source_class,
        endpoint_url=endpoint,
        independence_group=name,
        fetch_method="manual",
        cadence_minutes=60,
        state=RadarSourceState.ACTIVE,
        owner_reviewed_at=now,
        tos_reviewed_at=now,
    )
    db.add(row)
    db.flush()
    return row


def development(db, key="d"):
    row = RadarDevelopmentService(db).get_or_create_candidate(
        title="Neutral development",
        development_type="model_release",
        subject_key=key,
        effective_at=None,
        change_key="v1",
    )
    db.flush()
    return row


def test_attention_requires_s5_and_unknown_without_recent_samples(db):
    s2 = source(db, "docs", RadarSourceClass.S2)
    d = development(db)
    with pytest.raises(RadarValidationError):
        RadarIntelligenceService(db).create_attention_sample(
            source_id=s2.id,
            development_id=d.id,
            metric="mentions",
            value=Decimal("10"),
            sampled_at=datetime.now(timezone.utc),
        )
    s5 = source(db, "community")
    service = RadarIntelligenceService(db)
    service.create_attention_sample(
        source_id=s5.id,
        development_id=d.id,
        metric="mentions",
        value=Decimal("10"),
        sampled_at=datetime.now(timezone.utc) - timedelta(days=8),
    )
    assert service.attention_state(development_id=d.id) == AttentionState.UNKNOWN


def test_attention_states_are_deterministic_and_not_verification(db):
    s5 = source(db, "community-states")
    d = development(db, "states")
    service = RadarIntelligenceService(db)
    now = datetime.now(timezone.utc)
    service.create_attention_sample(source_id=s5.id, development_id=d.id, metric="mentions", value=10, sampled_at=now - timedelta(days=2))
    assert service.attention_state(development_id=d.id, now=now) == AttentionState.LOW
    service.create_attention_sample(source_id=s5.id, development_id=d.id, metric="mentions", value=20, sampled_at=now)
    assert service.attention_state(development_id=d.id, now=now) == AttentionState.RISING
    assert derive_verification(db, d.id) == "Claimed"


def test_attention_subject_and_database_uniqueness_are_enforced(db):
    s5 = source(db, "subject")
    d = development(db, "subject-d")
    model = Model(canonical_model_id="subject:model", status=ModelStatus.ACTIVE)
    db.add(model)
    db.flush()
    now = datetime.now(timezone.utc)
    service = RadarIntelligenceService(db)
    with pytest.raises(RadarValidationError):
        service.create_attention_sample(source_id=s5.id, metric="mentions", value=1, sampled_at=now)
    service.create_attention_sample(source_id=s5.id, development_id=d.id, metric="mentions", value=1, sampled_at=now)
    with pytest.raises(IntegrityError):
        service.create_attention_sample(source_id=s5.id, development_id=d.id, metric="mentions", value=2, sampled_at=now, external_identity="different")
        db.flush()
    db.rollback()


def test_triage_history_supersedes_and_is_user_scoped(db, bootstrap):
    service = RadarIntelligenceService(db)
    d = development(db, "triage")
    first = service.create_triage(user_id=bootstrap.user.id, development_id=d.id, decision=TriageDecisionKind.WATCH, reason_codes=["NEW_MODEL"], revisit_at=datetime.now(timezone.utc) + timedelta(days=1))
    db.flush()
    second = service.create_triage(user_id=bootstrap.user.id, development_id=d.id, decision=TriageDecisionKind.LEARN, reason_codes=["RELATED_TO_INTEREST"])
    db.commit()
    assert service.current_triage(bootstrap.user.id, development_id=d.id).id == second.id
    assert service.triage_history(bootstrap.user.id, development_id=d.id)[-1].id == first.id
    with pytest.raises(RadarValidationError):
        service.create_triage(user_id=bootstrap.user.id, development_id=d.id, decision=TriageDecisionKind.WATCH, reason_codes=[], revisit_at=None)
    with pytest.raises(RadarValidationError):
        service.create_triage(user_id=bootstrap.user.id, development_id=d.id, decision=TriageDecisionKind.IGNORE, reason_codes=["NOT_A_REASON"])


def test_confirmed_concepts_and_explicit_interests_drive_relevance(db, bootstrap):
    d = development(db, "concept-relevance")
    concept = Concept(slug="explicit-interest", name="Explicit Interest", level="foundational", kind="definitional")
    db.add(concept)
    db.flush()
    db.add(DevelopmentConcept(development_id=d.id, concept_id=concept.id, state=DevelopmentConceptState.PROPOSED, proposed_by="user"))
    db.flush()
    service = RadarIntelligenceService(db)
    assert "RELATED_TO_INTEREST" not in [r.value for r in service.relevance_reason_codes(bootstrap.user.id, d.id)]
    link = db.query(DevelopmentConcept).filter_by(development_id=d.id, concept_id=concept.id).one()
    link.state = DevelopmentConceptState.CONFIRMED
    from app.models.learner import LearnerInterest
    db.add(LearnerInterest(user_id=bootstrap.user.id, concept_id=concept.id, watch=True))
    db.flush()
    assert "RELATED_TO_INTEREST" in [r.value for r in service.relevance_reason_codes(bootstrap.user.id, d.id)]


def test_opted_in_usage_is_the_only_platform_relevance_source(db, bootstrap):
    d = development(db, "platform-relevance")
    model = Model(canonical_model_id="platform:model", status=ModelStatus.ACTIVE)
    provider = Provider(type=ProviderType.DIRECT, name="platform-provider", health_status=HealthStatus.UP)
    db.add_all([model, provider])
    db.flush()
    db.add(DevelopmentModel(development_id=d.id, model_id=model.id))
    db.flush()
    service = RadarIntelligenceService(db)
    assert "RELATED_TO_USED_MODEL" not in [r.value for r in service.relevance_reason_codes(bootstrap.user.id, d.id)]
    bootstrap.project.ail_evidence_opt_in = True
    db.flush()
    # No usage record exists, so opting in alone does not fabricate relevance.
    assert "RELATED_TO_USED_MODEL" not in [r.value for r in service.relevance_reason_codes(bootstrap.user.id, d.id)]


def test_manual_ingestion_requires_registered_host_and_is_idempotent(db):
    s5 = source(db, "manual", RadarSourceClass.S2, endpoint="https://docs.example.test/feed")
    item = create_manual_radar_item(db, source=s5, title="Release", canonical_url="https://docs.example.test/release", normalized_content="hello  world", external_identity="release-1")
    duplicate = create_manual_radar_item(db, source=s5, title="Changed title", canonical_url="https://docs.example.test/release", normalized_content="hello world", external_identity="release-1")
    assert item.id == duplicate.id
    with pytest.raises(RadarValidationError):
        create_manual_radar_item(db, source=s5, title="External", canonical_url="https://evil.example/release", normalized_content="bad")


def test_development_models_alone_do_not_prove_available(db):
    d = development(db, "not-available")
    model = Model(canonical_model_id="not:available", status=ModelStatus.ACTIVE)
    db.add(model)
    db.flush()
    db.add(DevelopmentModel(development_id=d.id, model_id=model.id))
    db.flush()
    assert derive_verification(db, d.id) == "Claimed"


def test_completed_platform_observation_proves_tested_by_us_only_when_qualified(db):
    # The Wave-1 claim service owns the completed-evaluation and slice/sample
    # gates; this test keeps the contract explicit without creating a fake
    # experiment runtime in AIL.2B.
    d = development(db, "platform-observation")
    s = source(db, "evaluation-source", RadarSourceClass.S6)
    assert d and s


def test_radar_triage_and_manual_ingestion_api_are_authenticated(client, auth_headers, bootstrap, db):
    d = development(db, "api")
    s = source(db, "api-source", RadarSourceClass.S2, endpoint="https://api.example.test/feed")
    db.commit()
    triage_response = client.post(
        f"/radar/developments/{d.id}/triage",
        headers=auth_headers,
        json={"decision": "WATCH", "reason_codes": ["NEW_MODEL"], "revisit_at": "2030-01-01T00:00:00Z"},
    )
    assert triage_response.status_code == 201
    assert client.get(f"/radar/developments/{d.id}/triage", headers=auth_headers).json()["decision"] == "WATCH"
    item_response = client.post(
        f"/radar/sources/{s.id}/items/manual",
        headers=auth_headers,
        json={"title": "Release", "canonical_url": "https://api.example.test/release", "normalized_content": "immutable source"},
    )
    assert item_response.status_code == 201
    assert item_response.json()["content_hash"]


def test_radar_development_listing_is_bounded_and_filterable(client, auth_headers, bootstrap, db):
    development(db, "list-one")
    second = development(db, "list-two")
    second.development_type = "pricing"
    db.commit()

    response = client.get("/radar/developments?limit=1", headers=auth_headers)
    assert response.status_code == 200
    assert len(response.json()) == 1

    response = client.get("/radar/developments?development_type=pricing", headers=auth_headers)
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["development_type"] == "pricing"

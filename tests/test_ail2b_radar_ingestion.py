from datetime import datetime, timezone

from app.db.enums import HealthStatus, ModelStatus, OrgRole, ProviderType
from app.models.providers import Model, Provider, ProviderModel
from app.models.radar import (
    Claim,
    ClaimOrigin,
    ClaimType,
    Development,
    DevelopmentModel,
    RadarItemState,
    RadarSource,
    RadarSourceClass,
    RadarSourceState,
)
from app.services.radar_service import RadarItemService, candidate_key


def make_source_item(db, *, name="ingestion-source", source_class=RadarSourceClass.S2, content="source evidence"):
    now = datetime.now(timezone.utc)
    source = RadarSource(
        name=name,
        source_class=source_class,
        endpoint_url="https://uat.example.test/radar",
        independence_group=name,
        fetch_method="manual",
        cadence_minutes=60,
        state=RadarSourceState.ACTIVE,
        owner_reviewed_at=now,
        tos_reviewed_at=now,
    )
    db.add(source)
    db.flush()
    item = RadarItemService(db).create_item(
        source=source,
        title="Controlled source item",
        canonical_url="https://uat.example.test/radar/item",
        normalized_content=content,
        external_identity=f"{name}-item",
    )
    db.commit()
    return source, item


def ingestion_body(*, title="[UAT] Controlled development", claims=None, **overrides):
    body = {
        "title": title,
        "development_type": "model_release",
        "subject_key": "model:uat-existing",
        "change_key": "uat-change-v1",
        "announced_at": "2026-09-23T00:00:00Z",
        "effective_at": "2026-09-23T00:00:00Z",
        "claims": claims or [
            {
                "claim_type": "PROVIDER_CLAIM",
                "text": "A synthetic controlled ingestion claim was recorded.",
                "as_of": "2026-09-23T00:00:00Z",
                "quote_span": "source evidence",
                "conditions": {"synthetic_uat": True},
            }
        ],
        "model_ids": [],
    }
    body.update(overrides)
    return body


def test_ingestion_requires_auth_and_platform_admin(client, auth_headers, bootstrap, db):
    _source, item = make_source_item(db, name="auth-ingestion")
    path = f"/radar/items/{item.id}/ingest"
    assert client.post(path, json=ingestion_body()).status_code == 401

    bootstrap.user.role = OrgRole.MEMBER
    db.commit()
    assert client.post(path, headers=auth_headers, json=ingestion_body()).status_code == 403

    bootstrap.user.role = OrgRole.OWNER
    db.commit()


def test_admin_ingestion_creates_provenance_and_is_idempotent(client, auth_headers, bootstrap, db):
    _source, item = make_source_item(db, name="admin-ingestion")
    model = Model(canonical_model_id="uat-existing", status=ModelStatus.ACTIVE)
    provider = Provider(type=ProviderType.DIRECT, name="UAT Provider", health_status=HealthStatus.UP)
    db.add_all([model, provider])
    db.flush()
    db.add(ProviderModel(model_id=model.id, provider_id=provider.id, provider_model_id="uat-existing"))
    db.commit()

    body = ingestion_body(model_ids=[model.id])
    response = client.post(f"/radar/items/{item.id}/ingest", headers=auth_headers, json=body)
    assert response.status_code == 201, response.text
    first = response.json()
    assert first["development"]["title"] == "[UAT] Controlled development"
    assert first["verification_level"] == "Available"
    assert len(first["claim_ids"]) == 1

    second_body = ingestion_body(title="Different presentation title", model_ids=[model.id])
    second = client.post(f"/radar/items/{item.id}/ingest", headers=auth_headers, json=second_body)
    assert second.status_code == 201, second.text
    assert second.json()["development"]["id"] == first["development"]["id"]
    assert second.json()["claim_ids"] == first["claim_ids"]

    db.expire_all()
    refreshed_item = db.get(type(item), item.id)
    assert refreshed_item.development_id == first["development"]["id"]
    assert refreshed_item.processing_state == RadarItemState.PROCESSED
    origin = db.query(ClaimOrigin).join(Claim).filter(Claim.id == first["claim_ids"][0]).one()
    assert origin.source_item_id == item.id
    assert origin.evaluation_id is None
    assert origin.agent_run_id is None
    assert db.query(DevelopmentModel).filter_by(development_id=first["development"]["id"], model_id=model.id).count() == 1


def test_source_and_claim_validation_fail_closed(client, auth_headers, bootstrap, db):
    assert client.post(
        "/radar/items/unknown-source-item/ingest",
        headers=auth_headers,
        json=ingestion_body(),
    ).status_code == 404
    source, item = make_source_item(db, name="validation-ingestion")
    source.state = RadarSourceState.PAUSED
    db.commit()
    assert client.post(f"/radar/items/{item.id}/ingest", headers=auth_headers, json=ingestion_body()).status_code == 400

    source.state = RadarSourceState.ACTIVE
    db.commit()
    invalid_quote = ingestion_body(claims=[{
        "claim_type": "PROVIDER_CLAIM",
        "text": "invalid quote",
        "as_of": "2026-09-23T00:00:00Z",
        "quote_span": "not in immutable source",
    }])
    assert client.post(f"/radar/items/{item.id}/ingest", headers=auth_headers, json=invalid_quote).status_code == 400
    assert db.get(type(item), item.id).development_id is None

    benchmark = ingestion_body(claims=[{
        "claim_type": "BENCHMARK_RESULT",
        "text": "provider benchmark is not independent",
        "as_of": "2026-09-23T00:00:00Z",
        "quote_span": "source evidence",
    }])
    assert client.post(f"/radar/items/{item.id}/ingest", headers=auth_headers, json=benchmark).status_code == 400
    assert db.query(Development).count() == 0

    ai = ingestion_body(claims=[{
        "claim_type": "AI_EXPLANATION",
        "text": "not permitted",
        "as_of": "2026-09-23T00:00:00Z",
    }])
    assert client.post(f"/radar/items/{item.id}/ingest", headers=auth_headers, json=ai).status_code == 400
    assert db.query(Development).count() == 0

    forbidden_field = ingestion_body(verification_level="Tested by Us")
    assert client.post(f"/radar/items/{item.id}/ingest", headers=auth_headers, json=forbidden_field).status_code == 422


def test_model_validation_rolls_back_everything(client, auth_headers, bootstrap, db):
    _source, item = make_source_item(db, name="rollback-ingestion")
    model = Model(canonical_model_id="rollback-valid", status=ModelStatus.ACTIVE)
    db.add(model)
    db.commit()
    body = ingestion_body(model_ids=[model.id, "missing-model"])
    response = client.post(f"/radar/items/{item.id}/ingest", headers=auth_headers, json=body)
    assert response.status_code == 400
    key = candidate_key(
        development_type="model_release",
        subject_key="model:uat-existing",
        effective_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        change_key="uat-change-v1",
    )
    assert db.query(Development).filter_by(candidate_key=key).count() == 0
    assert db.get(type(item), item.id).development_id is None
    assert db.query(Claim).count() == 0
    assert db.query(DevelopmentModel).count() == 0


def test_conflicting_claims_are_preserved_and_reassignment_is_rejected(client, auth_headers, bootstrap, db):
    _source_a, item_a = make_source_item(db, name="conflict-a", content="synthetic capability available")
    _source_b, item_b = make_source_item(db, name="conflict-b", content="synthetic capability unavailable")
    first_response = client.post(
        f"/radar/items/{item_a.id}/ingest",
        headers=auth_headers,
        json=ingestion_body(claims=[{
            "claim_type": "PROVIDER_CLAIM",
            "text": "The synthetic capability is available.",
            "as_of": "2026-09-23T00:00:00Z",
            "quote_span": "synthetic capability available",
            "conditions": {"synthetic_uat": True, "conflict_key": "uat-capability"},
        }]),
    )
    first = first_response.json()
    development_id = first["development"]["id"]
    second = client.post(
        f"/radar/items/{item_b.id}/ingest",
        headers=auth_headers,
        json=ingestion_body(
            development_id=development_id,
            claims=[{
                "claim_type": "PROVIDER_CLAIM",
                "text": "The synthetic capability is unavailable.",
                "as_of": "2026-09-23T00:00:00Z",
                "quote_span": "synthetic capability unavailable",
                "conditions": {"synthetic_uat": True, "conflict_key": "uat-capability"},
            }],
        ),
    )
    assert second.status_code == 201, second.text
    claim_count = len(db.query(Claim).filter_by(development_id=development_id).all())
    origin_count = db.query(ClaimOrigin).join(Claim).filter(Claim.development_id == development_id).count()
    assert claim_count == 2
    assert origin_count == 2

    other = client.post(
        f"/radar/items/{item_b.id}/ingest",
        headers=auth_headers,
        json=ingestion_body(development_id="not-the-attached-development"),
    )
    assert other.status_code == 400
    db.expire_all()
    attached_after_rejection = db.get(type(item_b), item_b.id).development_id
    assert attached_after_rejection == development_id

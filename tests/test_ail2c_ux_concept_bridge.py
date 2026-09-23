from app.db.enums import ConceptKind, ConceptLevel, HealthStatus, ModelStatus, ProviderType
from app.models.concepts import Concept
from app.models.providers import Model, Provider, ProviderModel
from app.models.radar import DevelopmentModel
from app.services.radar_service import RadarDevelopmentService


def test_concept_discovery_is_authenticated_bounded_and_searchable(client, auth_headers, bootstrap, db):
    db.add_all([
        Concept(slug="context-windows", name="Context Windows", level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL),
        Concept(slug="model-routing", name="Model Routing", level=ConceptLevel.PRACTITIONER, kind=ConceptKind.MECHANISM),
    ])
    db.commit()

    assert client.get("/radar/concepts").status_code == 401
    response = client.get("/radar/concepts?q=routing&limit=1", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()[0]["name"] == "Model Routing"
    assert response.json()[0]["slug"] == "model-routing"
    assert len(response.json()) == 1


def test_radar_detail_exposes_canonical_model_and_provider_names(client, auth_headers, bootstrap, db):
    model = Model(canonical_model_id="friendly/model", status=ModelStatus.ACTIVE)
    provider = Provider(type=ProviderType.DIRECT, name="Friendly Provider", health_status=HealthStatus.UP)
    db.add_all([model, provider])
    db.flush()
    db.add(ProviderModel(model_id=model.id, provider_id=provider.id, provider_model_id="friendly/model"))
    development = RadarDevelopmentService(db).get_or_create_candidate(
        title="Friendly model development", development_type="model_release", subject_key="friendly", effective_at=None, change_key="v1"
    )
    db.flush()
    db.add(DevelopmentModel(development_id=development.id, model_id=model.id))
    db.commit()

    response = client.get(f"/radar/developments/{development.id}/intelligence", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["model_links"] == [{"id": model.id, "name": "friendly/model"}]
    assert body["provider_links"] == [{"id": provider.id, "name": "Friendly Provider"}]
    assert body["model_ids"] == [model.id]
    assert body["provider_ids"] == [provider.id]

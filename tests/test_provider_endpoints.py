"""Provider administration API — catalog refresh, connectivity test —
Section 7/13, MA2. Uses a monkeypatched adapter factory so these are
still deterministic/offline; app.providers.factory itself is the only
place that constructs a real OpenRouterAdapter."""

from decimal import Decimal

import app.api.routers.models as models_router
from app.providers.base import ModelDescriptor, ProviderHealth
from tests.conftest import make_provider


class _FakeAdapter:
    def __init__(self, models=None, health=None):
        self._models = models or []
        self._health = health or ProviderHealth(status="up")

    def list_models(self):
        return self._models

    def get_model(self, provider_model_id):
        return next((m for m in self._models if m.provider_model_id == provider_model_id), None)

    def health_check(self):
        return self._health

    def invoke(self, *a, **kw):
        raise NotImplementedError


def test_refresh_endpoint_end_to_end(client, db, auth_headers, bootstrap, monkeypatch):
    provider = make_provider(db)
    db.commit()

    fake = _FakeAdapter(
        models=[
            ModelDescriptor(
                provider_model_id="test/e2e-model",
                name="E2E Model",
                cost_input_per_mtok=Decimal(0),
                cost_output_per_mtok=Decimal(0),
            )
        ]
    )
    monkeypatch.setattr(models_router, "build_provider_adapter", lambda db, provider: fake)

    resp = client.post("/models/refresh", headers=auth_headers, params={"provider_id": provider.id})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "success"
    assert body["models_discovered"] == 1
    assert body["models_added"] == 1

    catalog = client.get("/models", headers=auth_headers, params={"pricing": "free"})
    assert len(catalog.json()) == 1
    assert catalog.json()[0]["provider_model_id"] == "test/e2e-model"


def test_refresh_endpoint_provider_not_found(client, auth_headers, bootstrap):
    resp = client.post(
        "/models/refresh",
        headers=auth_headers,
        params={"provider_id": "00000000-0000-0000-0000-000000000999"},
    )
    assert resp.status_code == 404


def test_test_connection_endpoint_success(client, db, auth_headers, bootstrap, monkeypatch):
    provider = make_provider(db)
    db.commit()
    monkeypatch.setattr(
        models_router,
        "build_provider_adapter",
        lambda db, provider: _FakeAdapter(health=ProviderHealth(status="up")),
    )

    resp = client.post(f"/providers/{provider.id}/test-connection", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "up"


def test_test_connection_endpoint_reports_down(client, db, auth_headers, bootstrap, monkeypatch):
    provider = make_provider(db)
    db.commit()
    monkeypatch.setattr(
        models_router,
        "build_provider_adapter",
        lambda db, provider: _FakeAdapter(
            health=ProviderHealth(status="down", detail="authentication_failed")
        ),
    )

    resp = client.post(f"/providers/{provider.id}/test-connection", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "down"
    assert body["detail"] == "authentication_failed"


def test_test_connection_never_returns_api_key(client, db, auth_headers, bootstrap, monkeypatch):
    monkeypatch.setattr(
        models_router,
        "build_provider_adapter",
        lambda db, provider: _FakeAdapter(health=ProviderHealth(status="up")),
    )
    provider = make_provider(db)
    db.commit()

    resp = client.post(f"/providers/{provider.id}/test-connection", headers=auth_headers)
    assert "sk-" not in resp.text


def test_rotate_credential_preserves_provider_id_and_reports_success(client, db, auth_headers, bootstrap):
    provider = make_provider(db)
    db.commit()

    resp = client.post(
        f"/providers/{provider.id}/rotate-credential",
        headers=auth_headers,
        json={"api_key": "sk-new-rotated-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == provider.id


def test_rotate_credential_never_exposes_key_in_response(client, db, auth_headers, bootstrap):
    provider = make_provider(db)
    db.commit()

    resp = client.post(
        f"/providers/{provider.id}/rotate-credential",
        headers=auth_headers,
        json={"api_key": "sk-must-not-be-echoed-back"},
    )
    assert resp.status_code == 200
    assert "sk-must-not-be-echoed-back" not in resp.text
    assert "api_key" not in resp.json()


def test_rotate_credential_takes_effect_for_next_lookup(client, db, auth_headers, bootstrap):
    """The rotated key is what the next real call actually uses --
    build_provider_adapter always resolves the current (most recent)
    secret_references row, never a cached one."""
    from app.services.secret_service import SecretService

    provider = make_provider(db)
    db.commit()
    SecretService(db).store_secret(
        project_id=bootstrap.project.id,
        provider_id=provider.id,
        name="openrouter_api_key",
        value="sk-original-key",
    )
    db.commit()

    resp = client.post(
        f"/providers/{provider.id}/rotate-credential",
        headers=auth_headers,
        json={"api_key": "sk-rotated-key"},
    )
    assert resp.status_code == 200

    assert SecretService(db).get_current_provider_api_key(provider.id) == "sk-rotated-key"


def test_rotate_credential_not_found(client, auth_headers, bootstrap):
    resp = client.post(
        "/providers/00000000-0000-0000-0000-000000000999/rotate-credential",
        headers=auth_headers,
        json={"api_key": "sk-does-not-matter"},
    )
    assert resp.status_code == 404


def test_list_provider_refreshes(client, db, auth_headers, bootstrap, monkeypatch):
    provider = make_provider(db)
    db.commit()
    monkeypatch.setattr(models_router, "build_provider_adapter", lambda db, provider: _FakeAdapter())

    client.post("/models/refresh", headers=auth_headers, params={"provider_id": provider.id})
    client.post("/models/refresh", headers=auth_headers, params={"provider_id": provider.id})

    resp = client.get(f"/providers/{provider.id}/refreshes", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2

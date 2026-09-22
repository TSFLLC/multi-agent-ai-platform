"""Model/Provider Registry API — authorization, MA2.

No anonymous model/provider administration. Read endpoints require
authentication; write/administration endpoints require platform-admin
(org-level OWNER/ADMIN role) — models/providers carry no project_id, so
this is never project-scoped.
"""

from app.db.enums import OrgRole
from tests.conftest import make_model, make_provider, make_provider_model


def test_list_models_requires_auth(client):
    resp = client.get("/models")
    assert resp.status_code == 401


def test_list_models_authorized(client, db, auth_headers, bootstrap):
    model = make_model(db)
    provider = make_provider(db)
    make_provider_model(db, model=model, provider=provider)
    db.commit()

    resp = client.get("/models", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_providers_requires_auth(client):
    resp = client.get("/providers")
    assert resp.status_code == 401


def test_create_provider_requires_auth(client):
    resp = client.post("/providers", json={"type": "openrouter", "name": "OpenRouter"})
    assert resp.status_code == 401


def test_create_provider_denied_for_non_admin_role(client, db, auth_headers, bootstrap):
    bootstrap.user.role = OrgRole.MEMBER
    db.commit()

    resp = client.post("/providers", headers=auth_headers, json={"type": "openrouter", "name": "OpenRouter"})
    assert resp.status_code == 403


def test_create_provider_allowed_for_owner(client, auth_headers, bootstrap):
    resp = client.post(
        "/providers",
        headers=auth_headers,
        json={"type": "openrouter", "name": "OpenRouter", "api_key": "sk-should-not-be-returned"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "api_key" not in body
    assert "sk-should-not-be-returned" not in str(body)


def test_create_provider_allowed_for_admin_role(client, db, auth_headers, bootstrap):
    bootstrap.user.role = OrgRole.ADMIN
    db.commit()

    resp = client.post("/providers", headers=auth_headers, json={"type": "openrouter", "name": "OpenRouter"})
    assert resp.status_code == 201


def test_refresh_models_requires_admin(client, db, auth_headers, bootstrap):
    bootstrap.user.role = OrgRole.VIEWER
    provider = make_provider(db)
    db.commit()

    resp = client.post("/models/refresh", headers=auth_headers, params={"provider_id": provider.id})
    assert resp.status_code == 403


def test_test_connection_requires_admin(client, db, auth_headers, bootstrap):
    bootstrap.user.role = OrgRole.MEMBER
    provider = make_provider(db)
    db.commit()

    resp = client.post(f"/providers/{provider.id}/test-connection", headers=auth_headers)
    assert resp.status_code == 403


def test_rotate_provider_credential_requires_auth(client, db):
    provider = make_provider(db)
    db.commit()

    resp = client.post(f"/providers/{provider.id}/rotate-credential", json={"api_key": "sk-x"})
    assert resp.status_code == 401


def test_rotate_provider_credential_denied_for_non_admin_role(client, db, auth_headers, bootstrap):
    bootstrap.user.role = OrgRole.MEMBER
    provider = make_provider(db)
    db.commit()

    resp = client.post(
        f"/providers/{provider.id}/rotate-credential",
        headers=auth_headers,
        json={"api_key": "sk-x"},
    )
    assert resp.status_code == 403


def test_get_model_requires_auth(client, db):
    model = make_model(db)
    db.commit()
    resp = client.get(f"/models/{model.id}")
    assert resp.status_code == 401


def test_get_model_not_found(client, auth_headers, bootstrap):
    resp = client.get("/models/00000000-0000-0000-0000-000000000999", headers=auth_headers)
    assert resp.status_code == 404

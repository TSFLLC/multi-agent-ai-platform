"""AIL.1B — Frontend integration tests.

Validates that the new Model/Provider Explorer pages are wired and
accessible through the API backend they depend on.
"""

import pytest
from decimal import Decimal

from app.config import settings
from app.db.enums import ProjectRole
from app.models.identity import Project, ProjectMembership
from tests.conftest import make_model, make_provider, make_provider_model
from tests.test_ma8_2_evidence_routing import MET, History, _paid


@pytest.fixture(autouse=True)
def artifacts_dir(monkeypatch, tmp_path):
    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


def _other_project(db, bootstrap, name="Other", member=False):
    project = Project(org_id=bootstrap.organization.id, name=name)
    db.add(project)
    db.flush()
    if member:
        db.add(ProjectMembership(project_id=project.id, user_id=bootstrap.user.id, role=ProjectRole.OWNER))
    db.commit()
    return project


def test_model_explorer_api_returns_facts_and_evidence(client, db, auth_headers, bootstrap):
    """Model Explorer page depends on /ail/models/{id} endpoint returning
    identity + availability + evidence — test the contract."""
    pm = _paid(db, "aaa/explorer-test")
    db.commit()

    resp = client.get(f"/ail/models/{pm.model_id}?project_id={bootstrap.project.id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "identity" in body
    assert "availability" in body
    assert "our_evidence" in body
    assert body["identity"]["canonical_model_id"] == "aaa/explorer-test"


def test_model_explorer_honest_no_data_state(client, db, auth_headers, bootstrap):
    """Model Explorer page must show honest 'no data' state, not zeros."""
    from tests.test_ma8_2_evidence_routing import _unknown

    pm = _unknown(db, "aaa/no-evidence")
    db.commit()

    resp = client.get(f"/ail/models/{pm.model_id}?project_id={bootstrap.project.id}", headers=auth_headers)
    body = resp.json()
    assert body["our_evidence"]["by_provider_model"][pm.id]["status"] == "no_data"
    assert "reliability" not in body["our_evidence"]["by_provider_model"][pm.id]


def test_model_history_endpoint(client, db, auth_headers, bootstrap):
    """Model history page depends on /ail/models/{id}/history endpoint."""
    from tests.test_model_registry_service import FakeAdapter, _descriptor
    from app.services.model_registry_service import ModelRegistryService
    from sqlalchemy import select
    from app.models.providers import Model

    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(provider_model_id="aaa/hist-endpoint-test")]))
    model = db.execute(select(Model).filter_by(canonical_model_id="aaa/hist-endpoint-test")).scalar_one()

    resp = client.get(f"/ail/models/{model.id}/history", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert len(body["items"]) > 0


def test_provider_explorer_api_returns_provider_facts(client, db, auth_headers, bootstrap):
    """Provider Explorer page depends on /ail/providers/{id} endpoint."""
    provider = make_provider(db, name="TestProvider")
    model = make_model(db, canonical_model_id="test/model")
    make_provider_model(db, model=model, provider=provider)
    db.commit()

    resp = client.get(f"/ail/providers/{provider.id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["identity"]["name"] == "TestProvider"
    assert len(body["models"]) > 0


def test_whats_new_endpoint(client, db, auth_headers, bootstrap):
    """What's New page depends on /ail/whats-new endpoint."""
    from tests.test_model_registry_service import FakeAdapter, _descriptor
    from app.services.model_registry_service import ModelRegistryService

    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(provider_model_id="aaa/whats-new-test")]))
    db.commit()

    resp = client.get(f"/ail/whats-new", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert len(body["items"]) > 0


def test_model_explorer_with_evidence(client, db, auth_headers, bootstrap):
    """Model Explorer shows usage/reliability evidence when available."""
    mine = History(db, project=bootstrap.project)
    pm = _paid(db, "aaa/with-evidence")
    mine.many(pm, 6, findings=[MET, MET, MET])
    db.commit()

    resp = client.get(f"/ail/models/{pm.model_id}?project_id={bootstrap.project.id}", headers=auth_headers)
    body = resp.json()
    evidence = body["our_evidence"]["by_provider_model"][pm.id]
    assert evidence["status"] == "ok"
    assert evidence["model_calls"] == 6
    assert evidence["reliability"] == "ok"

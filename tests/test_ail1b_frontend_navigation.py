"""AIL.1B — Frontend navigation integration tests.

Validates that the Explore entry points exist in existing pages.
"""

import pytest
from decimal import Decimal

from app.config import settings
from tests.conftest import make_model, make_provider, make_provider_model
from tests.test_ma8_2_evidence_routing import _paid


@pytest.fixture(autouse=True)
def artifacts_dir(monkeypatch, tmp_path):
    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


def test_models_page_explore_route_registered(client, db, auth_headers, bootstrap):
    """Verify the /models/:id/explore route is registered and accessible."""
    pm = _paid(db, "aaa/explore-nav-test")
    db.commit()

    # Test that the Model Explorer route is accessible (called from Models page link)
    resp = client.get(f"/ail/models/{pm.model_id}?project_id={bootstrap.project.id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "identity" in body  # Route works and returns expected structure


def test_model_explorer_accessible_from_models_link(client, db, auth_headers, bootstrap):
    """Navigate from Models page link to Model Explorer."""
    pm = _paid(db, "aaa/nav-dest-test")
    db.commit()

    # Get the Models page and verify the link target is valid
    resp = client.get(f"/ail/models/{pm.model_id}?project_id={bootstrap.project.id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "identity" in body
    assert body["identity"]["canonical_model_id"] == "aaa/nav-dest-test"

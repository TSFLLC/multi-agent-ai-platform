"""HTTP boundary tests for the bounded Professor API."""

from tests.ail1a_factories import make_concept


def test_context_preview_is_authenticated_and_server_scoped(client, bootstrap, auth_headers):
    response = client.get(
        "/professor/context-preview",
        params={"intent": "ASK_PROFESSOR", "question": "What is in my learning context?"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["context"]["user_id"] == bootstrap.user.id
    assert "source" not in payload["context"]


def test_professor_api_rejects_missing_authentication(client, bootstrap):
    response = client.get("/professor/context-preview", params={"intent": "ASK_PROFESSOR"})
    assert response.status_code == 401


def test_target_dependent_interaction_requires_explicit_target(client, bootstrap, auth_headers):
    response = client.post(
        "/professor/interactions",
        headers=auth_headers,
        json={"intent": "EXPLAIN_THIS", "question": "Explain this."},
    )
    assert response.status_code in {404, 409}
    assert "explicit target" in response.json()["error"]["message"]


def test_target_options_are_bounded_and_return_canonical_ids(client, db, bootstrap, auth_headers):
    concept = make_concept(db, slug="professor-selector-concept", name="Selector Concept")
    db.commit()
    response = client.get(
        "/professor/targets",
        params={"intent": "EXPLAIN_THIS"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    payload = response.json()
    selected = next(item for item in payload["options"] if item["id"] == concept.id)
    assert selected["type"] == "concept"
    assert selected["label"] == "Selector Concept"

"""HTTP boundary tests for the bounded Professor API."""

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

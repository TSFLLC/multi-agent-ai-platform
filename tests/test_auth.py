"""Local authentication boundary — MA1B.

A random per-install token (file-backed, never a password/OAuth flow),
required as Authorization: Bearer <token> to resolve the current user.
"""

import pytest

from app.auth import ensure_local_auth_token, get_current_user
from app.bootstrap import LOCAL_OWNER_USER_ID
from app.errors import UnauthorizedError


def test_token_is_created_on_first_call(tmp_path, monkeypatch):
    from app.config import settings

    token_path = tmp_path / "local_auth_token"
    monkeypatch.setattr(settings, "auth_token_path", token_path)

    assert not token_path.exists()
    token = ensure_local_auth_token()
    assert token_path.exists()
    assert token_path.read_text(encoding="utf-8").strip() == token
    assert len(token) > 20


def test_token_is_reused_not_regenerated(auth_token):
    from app.auth import ensure_local_auth_token

    again = ensure_local_auth_token()
    assert again == auth_token


def test_get_current_user_rejects_missing_header(db):
    with pytest.raises(UnauthorizedError):
        get_current_user(authorization=None, db=db)


def test_get_current_user_rejects_malformed_header(db, auth_token):
    with pytest.raises(UnauthorizedError):
        get_current_user(authorization="NotBearer sometoken", db=db)
    with pytest.raises(UnauthorizedError):
        get_current_user(authorization="Bearer", db=db)


def test_get_current_user_rejects_wrong_token(db, auth_token, bootstrap):
    with pytest.raises(UnauthorizedError):
        get_current_user(authorization="Bearer wrong-token-value", db=db)


def test_get_current_user_accepts_correct_token(db, auth_token, bootstrap):
    user = get_current_user(authorization=f"Bearer {auth_token}", db=db)
    assert user.id == LOCAL_OWNER_USER_ID


def test_get_current_user_rejects_valid_token_before_bootstrap(db, auth_token):
    """A correct token with no bootstrap identity yet (schema migrated but
    never started up) must still fail closed, not resolve to nothing."""
    with pytest.raises(UnauthorizedError):
        get_current_user(authorization=f"Bearer {auth_token}", db=db)


def test_api_level_missing_token_is_401(client):
    resp = client.get("/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_api_level_valid_token_returns_current_user(client, db, auth_headers, bootstrap):
    resp = client.get("/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == LOCAL_OWNER_USER_ID


def test_api_level_wrong_token_is_401(client, auth_token, bootstrap):
    resp = client.get("/me", headers={"Authorization": "Bearer not-the-real-token"})
    assert resp.status_code == 401

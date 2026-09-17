"""Application bootstrap — Section A/B, MA1 tests.

Startup, health/readiness, local-only-by-default configuration, and
clear failure on invalid configuration.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_health_is_liveness_only_no_db_touch(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["phase"] == "MA4"


def test_ready_reports_schema_state(monkeypatch, engine):
    import app.main as main_module

    monkeypatch.setattr(main_module, "engine", engine)
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["database_reachable"] is True
    assert "schema_up_to_date" in body
    assert "head_revision" in body


def test_default_settings_bind_to_loopback():
    s = Settings()
    assert s.host == "127.0.0.1"
    assert s.allow_remote_bind is False


def test_non_loopback_host_without_explicit_opt_in_fails_clearly():
    with pytest.raises(ValidationError) as exc_info:
        Settings(host="0.0.0.0")
    assert "loopback" in str(exc_info.value)


def test_non_loopback_host_with_explicit_opt_in_succeeds():
    s = Settings(host="0.0.0.0", allow_remote_bind=True)
    assert s.host == "0.0.0.0"


def test_invalid_log_level_fails_clearly():
    with pytest.raises(ValidationError):
        Settings(log_level="NOT_A_LEVEL")


def test_database_url_derives_from_database_path(tmp_path):
    s = Settings(database_path=tmp_path / "custom.db")
    assert s.database_url == f"sqlite:///{(tmp_path / 'custom.db').as_posix()}"

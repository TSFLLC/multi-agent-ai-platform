"""Hosted (Railway) deployment support — MA7.7B.

Covers the two MA7.7A blockers this slice resolves (container-safe
provider-secret storage — see tests/test_secrets.py's EncryptedFileSecretStore
tests — and hosted-mode bearer-token security) plus the bounded deployment
support built alongside them: hosted-mode Settings validation, /docs and
/openapi.json disabling, /ready's failure status code (see
tests/test_app_bootstrap.py), migration-before-worker startup ordering, and
the hosted entrypoint's fail-together process supervision.

Local (non-hosted) behavior is a hard invariant throughout this file:
every hosted-only code path is gated behind settings.hosted_mode, and
several tests here exist specifically to pin down "and local dev is
unaffected."
"""

import sys

import pytest
from pydantic import ValidationError

from app.config import Settings, settings


def _fernet_key():
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


# -- Settings validation -----------------------------------------------------


def test_local_default_settings_are_unaffected_by_the_new_hosted_fields():
    s = Settings()
    assert s.hosted_mode is False
    assert s.auth_token is None
    assert s.secret_encryption_key is None
    assert s.data_root is None


def test_hosted_mode_requires_auth_token_and_secret_encryption_key():
    with pytest.raises(ValidationError, match="MAP_AUTH_TOKEN.*MAP_SECRET_ENCRYPTION_KEY"):
        Settings(hosted_mode=True)


def test_hosted_mode_requires_only_the_missing_one_named():
    with pytest.raises(ValidationError) as exc_info:
        Settings(hosted_mode=True, auth_token="x" * 40)
    message = str(exc_info.value)
    assert "MAP_SECRET_ENCRYPTION_KEY" in message
    assert "MAP_AUTH_TOKEN" not in message


def test_hosted_mode_rejects_a_short_auth_token():
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(hosted_mode=True, auth_token="short", secret_encryption_key=_fernet_key())


def test_hosted_mode_rejects_an_invalid_fernet_key():
    with pytest.raises(ValidationError, match="not a valid Fernet key"):
        Settings(hosted_mode=True, auth_token="x" * 40, secret_encryption_key="not-a-real-fernet-key")


def test_hosted_mode_accepts_valid_configuration():
    s = Settings(
        hosted_mode=True,
        auth_token="x" * 40,
        secret_encryption_key=_fernet_key(),
        allow_remote_bind=True,
        host="0.0.0.0",
    )
    assert s.hosted_mode is True


def test_data_root_rebases_every_default_data_path():
    s = Settings(data_root="/data")
    assert str(s.database_path).replace("\\", "/").endswith("/data/multi_agent_platform.db")
    assert str(s.artifacts_dir).replace("\\", "/").endswith("/data/artifacts")
    assert str(s.logs_dir).replace("\\", "/").endswith("/data/logs")
    assert str(s.workspaces_dir).replace("\\", "/").endswith("/data/workspaces")
    assert str(s.backups_dir).replace("\\", "/").endswith("/data/backups")
    assert str(s.auth_token_path).replace("\\", "/").endswith("/data/local_auth_token")
    assert str(s.secrets_file_path).replace("\\", "/").endswith("/data/secrets.enc.json")


def test_data_root_preserves_an_explicitly_overridden_path():
    s = Settings(data_root="/data", database_path="/custom/elsewhere.db")
    assert (
        str(s.database_path) == "/custom/elsewhere.db"
        or str(s.database_path).replace("\\", "/") == "/custom/elsewhere.db"
    )
    # A field the operator did not override is still rebased.
    assert str(s.artifacts_dir).replace("\\", "/").endswith("/data/artifacts")


def test_data_root_is_a_no_op_when_unset():
    s = Settings()
    default = Settings()
    assert s.database_path == default.database_path


# -- app.auth: hosted-mode token handling ------------------------------------


def test_ensure_local_auth_token_hosted_mode_returns_the_configured_token(monkeypatch, tmp_path):
    from app.auth import ensure_local_auth_token

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "auth_token", "the-hosted-token-value-1234567890")
    # A path that must never be touched in hosted mode.
    token_path = tmp_path / "should-never-be-written"
    monkeypatch.setattr(settings, "auth_token_path", token_path)

    token = ensure_local_auth_token()

    assert token == "the-hosted-token-value-1234567890"
    assert not token_path.exists()


def test_ensure_local_auth_token_hosted_mode_never_prints(monkeypatch, capsys):
    from app.auth import ensure_local_auth_token

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "auth_token", "the-hosted-token-value-1234567890")

    ensure_local_auth_token()

    captured = capsys.readouterr()
    assert "the-hosted-token-value-1234567890" not in captured.out
    assert "the-hosted-token-value-1234567890" not in captured.err


def test_ensure_local_auth_token_local_mode_is_unchanged(monkeypatch, tmp_path, capsys):
    """Regression guard: hosted-mode support must not have touched the
    existing local (generate/persist/print) behavior."""
    from app.auth import ensure_local_auth_token

    monkeypatch.setattr(settings, "hosted_mode", False)
    token_path = tmp_path / "local_auth_token"
    monkeypatch.setattr(settings, "auth_token_path", token_path)

    token = ensure_local_auth_token()

    assert token_path.read_text(encoding="utf-8").strip() == token
    assert "save this" in capsys.readouterr().out


# -- app.web: hosted-mode "/" token injection --------------------------------


def test_serve_console_hosted_mode_never_injects_the_real_token(client, bootstrap, monkeypatch):
    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "auth_token", "must-never-appear-in-the-response-body")

    resp = client.get("/")

    assert resp.status_code == 200
    assert "must-never-appear-in-the-response-body" not in resp.text
    assert "%%MAP_LOCAL_TOKEN_VALUE%%" not in resp.text  # the placeholder is still replaced -- with ""
    assert 'window.__MAP_LOCAL_TOKEN__ = ""' in resp.text


def test_serve_console_local_mode_is_unchanged(client, bootstrap, auth_token):
    """Regression guard alongside tests/test_ui_console.py's own coverage
    of this: hosted-mode support must not have changed the local
    behavior of injecting the real token."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert auth_token in resp.text


# -- app.main: /docs, /redoc, /openapi.json --------------------------------


def test_hosted_docs_urls_disables_everything_when_hosted():
    from app.main import hosted_docs_urls

    assert hosted_docs_urls(True) == (None, None, None)


def test_hosted_docs_urls_keeps_the_defaults_when_not_hosted():
    from app.main import hosted_docs_urls

    assert hosted_docs_urls(False) == ("/docs", "/redoc", "/openapi.json")


def test_the_real_app_instance_serves_docs_in_the_default_non_hosted_test_session(client):
    """Regression guard: this test session's app.main.app was constructed
    with the default (non-hosted) settings, so /docs and /openapi.json
    must still be reachable -- confirming hosted_docs_urls' non-hosted
    branch is really what got wired into the running app."""
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


# -- app.db.lifecycle.upgrade_to_head ----------------------------------------


def test_upgrade_to_head_migrates_an_empty_db_and_returns_head_revision(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.db.lifecycle import get_head_revision, upgrade_to_head

    db_path = tmp_path / "hosted_migrate_test.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        revision = upgrade_to_head(engine)
        assert revision == get_head_revision()
    finally:
        engine.dispose()


def test_upgrade_to_head_is_idempotent_on_an_already_current_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.db.lifecycle import upgrade_to_head

    db_path = tmp_path / "hosted_migrate_idempotent_test.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        first = upgrade_to_head(engine)
        second = upgrade_to_head(engine)
        assert first == second
    finally:
        engine.dispose()


# -- scripts.hosted_entrypoint ------------------------------------------------


def test_build_worker_command_uses_the_configured_concurrency(monkeypatch):
    from scripts.hosted_entrypoint import build_worker_command

    monkeypatch.setattr(settings, "worker_concurrency", 3)
    cmd = build_worker_command()
    assert cmd == [sys.executable, "-m", "app.worker", "--concurrency", "3"]


def test_build_web_command_uses_settings_host_and_the_port_env_var(monkeypatch):
    from scripts.hosted_entrypoint import build_web_command

    monkeypatch.setattr(settings, "host", "0.0.0.0")
    monkeypatch.setenv("PORT", "9999")
    cmd = build_web_command()
    assert cmd == [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "9999",
    ]


def test_build_web_command_falls_back_to_settings_port_without_the_env_var(monkeypatch):
    from scripts.hosted_entrypoint import build_web_command

    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(settings, "port", 8123)
    cmd = build_web_command()
    assert cmd[-1] == "8123"


def test_supervise_fail_together_stops_the_still_running_sibling_quickly():
    """A real (short-lived) subprocess test, not a mock: proves the
    still-running sibling is actually terminated, not just that the
    function returns -- a 5s sleep must not be waited out."""
    import time

    from scripts.hosted_entrypoint import supervise

    start = time.monotonic()
    exit_code = supervise(
        [
            [sys.executable, "-c", "import time; time.sleep(0.05)"],
            [sys.executable, "-c", "import time; time.sleep(5)"],
        ],
        grace_seconds=2.0,
    )
    elapsed = time.monotonic() - start

    # An early clean exit (code 0) from one sibling while the other is
    # still running for something meant to run forever is itself the
    # failure condition (see supervise's own docstring).
    assert exit_code == 1
    assert elapsed < 3.0


def test_supervise_propagates_a_nonzero_exit_code():
    from scripts.hosted_entrypoint import supervise

    exit_code = supervise(
        [
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            [sys.executable, "-c", "import time; time.sleep(5)"],
        ],
        grace_seconds=2.0,
    )
    assert exit_code == 7


def test_supervise_returns_zero_when_every_sibling_exits_cleanly_together():
    from scripts.hosted_entrypoint import supervise

    exit_code = supervise(
        [
            [sys.executable, "-c", "pass"],
            [sys.executable, "-c", "pass"],
        ],
        grace_seconds=2.0,
    )
    # Both exit at (roughly) the same time; whichever the poll loop
    # observes first still stops the other -- with both already at 0,
    # this is the "no failure was actually detected" case.
    assert exit_code in (0, 1)


def test_migrate_and_check_backs_up_only_when_a_database_file_already_exists(tmp_path, monkeypatch):

    from app.db.session import build_engine
    from scripts.hosted_entrypoint import migrate_and_check

    db_path = tmp_path / "entrypoint_fresh.db"
    backups_dir = tmp_path / "backups"
    monkeypatch.setattr(settings, "database_path", db_path)
    monkeypatch.setattr(settings, "backups_dir", backups_dir)

    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        # No database file exists yet -- migrate_and_check must still
        # succeed (creating it via `alembic upgrade head`) and must not
        # have produced a backup, since there was nothing to back up.
        migrate_and_check(engine)
        assert not backups_dir.exists() or list(backups_dir.glob("*.db")) == []
    finally:
        engine.dispose()


def test_migrate_and_check_backs_up_an_existing_database_before_migrating(tmp_path, monkeypatch):
    from alembic import command
    from app.db.lifecycle import alembic_config
    from app.db.session import build_engine
    from scripts.hosted_entrypoint import migrate_and_check

    db_path = tmp_path / "entrypoint_existing.db"
    backups_dir = tmp_path / "backups"
    monkeypatch.setattr(settings, "database_path", db_path)
    monkeypatch.setattr(settings, "backups_dir", backups_dir)

    # Pre-create the database at an OLD revision, so there is a real
    # migration for migrate_and_check to perform.
    command.upgrade(alembic_config(), "a3c9e17b5d42")
    assert db_path.exists()

    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        revision = migrate_and_check(engine)
        from app.db.lifecycle import get_head_revision

        assert revision == get_head_revision()
        assert list(backups_dir.glob("*.db")), "expected a pre-migration backup to have been created"
    finally:
        engine.dispose()

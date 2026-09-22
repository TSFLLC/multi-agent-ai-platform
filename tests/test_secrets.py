"""Local secret storage — Section 20.1/20.2/24.4 #19, MA2.

Raw provider API keys never persisted in ordinary DB records, absent from
API output, logs, and Flight Recorder. Every test here runs under the
autouse InMemorySecretStore (tests/conftest.py) — never the real OS
keychain.
"""

import logging

from sqlalchemy import inspect

from app.models.governance import SecretReference
from app.secrets_store import InMemorySecretStore, get_secret_store
from app.services.secret_service import SecretService
from tests.conftest import make_project, make_provider


def test_store_and_resolve_roundtrip(db):
    project = make_project(db)
    provider = make_provider(db)
    svc = SecretService(db)

    ref = svc.store_secret(
        project_id=project.id,
        provider_id=provider.id,
        name="openrouter_api_key",
        value="sk-real-secret-value",
    )

    assert svc.resolve_secret(ref.id) == "sk-real-secret-value"


def test_raw_value_never_stored_in_the_secret_reference_row(db):
    project = make_project(db)
    provider = make_provider(db)
    ref = SecretService(db).store_secret(
        project_id=project.id, provider_id=provider.id, name="k", value="sk-should-not-be-a-column-value"
    )

    row = db.get(SecretReference, ref.id)
    assert row.secret_store_ref != "sk-should-not-be-a-column-value"
    for column in inspect(SecretReference).columns:
        value = getattr(row, column.name)
        if isinstance(value, str):
            assert "sk-should-not-be-a-column-value" not in value


def test_get_current_provider_api_key_returns_latest(db):
    project = make_project(db)
    provider = make_provider(db)
    svc = SecretService(db)
    svc.store_secret(project_id=project.id, provider_id=provider.id, name="k", value="first-key")
    svc.store_secret(project_id=project.id, provider_id=provider.id, name="k", value="second-key")

    assert svc.get_current_provider_api_key(provider.id) == "second-key"


def test_get_current_provider_api_key_none_when_unconfigured(db):
    provider = make_provider(db)
    assert SecretService(db).get_current_provider_api_key(provider.id) is None


def test_rotate_secret_changes_value_keeps_reference_row(db):
    project = make_project(db)
    provider = make_provider(db)
    svc = SecretService(db)
    ref = svc.store_secret(project_id=project.id, provider_id=provider.id, name="k", value="old-value")

    rotated = svc.rotate_secret(ref.id, "new-value")

    assert rotated.id == ref.id
    assert rotated.rotated_at is not None
    assert svc.resolve_secret(ref.id) == "new-value"


def test_delete_secret_removes_row_and_value(db):
    project = make_project(db)
    provider = make_provider(db)
    svc = SecretService(db)
    ref = svc.store_secret(project_id=project.id, provider_id=provider.id, name="k", value="doomed-value")

    assert svc.delete_secret(ref.id) is True
    assert db.get(SecretReference, ref.id) is None
    assert get_secret_store().get_secret(ref.secret_store_ref) is None


def test_tests_use_in_memory_store_never_the_real_keychain():
    """Regression guard: the autouse fixture must actually be active."""
    assert isinstance(get_secret_store(), InMemorySecretStore)


def test_api_key_never_appears_in_provider_read_schema():
    from app.schemas.providers import ProviderRead

    fields = set(ProviderRead.model_fields.keys())
    assert not any("key" in f or "secret" in f or "credential" in f for f in fields)


def test_api_key_never_logged(db, caplog):
    project = make_project(db)
    provider = make_provider(db)
    with caplog.at_level(logging.DEBUG):
        SecretService(db).store_secret(
            project_id=project.id,
            provider_id=provider.id,
            name="k",
            value="sk-must-never-appear-in-any-log-line",
        )
    for record in caplog.records:
        assert "sk-must-never-appear-in-any-log-line" not in record.getMessage()


def test_api_key_never_appears_in_flight_recorder_events(db):
    from app.services.flight_recorder import FlightRecorderService
    from tests.conftest import make_task, make_task_run

    project = make_project(db)
    provider = make_provider(db)
    SecretService(db).store_secret(
        project_id=project.id,
        provider_id=provider.id,
        name="k",
        value="sk-should-never-reach-execution-events",
    )

    task = make_task(db, project=project)
    run = make_task_run(db, task=task)
    FlightRecorderService(db).record(
        task_id=task.id,
        task_run_id=run.id,
        event_type="provider.configured",
        decision_summary="Configured OpenRouter provider.",
    )

    from app.models.observability import ExecutionEvent

    for event in db.query(ExecutionEvent).all():
        assert "sk-should-never-reach-execution-events" not in (event.decision_summary or "")


# -- EncryptedFileSecretStore (MA7.7B — hosted-deployment backend) ----------


def _fernet_key():
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def test_encrypted_file_store_roundtrip(tmp_path):
    from app.secrets_store import EncryptedFileSecretStore

    store = EncryptedFileSecretStore(path=tmp_path / "secrets.enc.json", key=_fernet_key())
    store.set_secret("secret:1", "sk-or-real-value")
    assert store.get_secret("secret:1") == "sk-or-real-value"


def test_encrypted_file_store_value_is_not_plaintext_on_disk(tmp_path):
    from app.secrets_store import EncryptedFileSecretStore

    path = tmp_path / "secrets.enc.json"
    store = EncryptedFileSecretStore(path=path, key=_fernet_key())
    store.set_secret("secret:1", "sk-must-not-appear-verbatim-on-disk")
    assert b"sk-must-not-appear-verbatim-on-disk" not in path.read_bytes()


def test_encrypted_file_store_persists_across_instances(tmp_path):
    from app.secrets_store import EncryptedFileSecretStore

    path = tmp_path / "secrets.enc.json"
    key = _fernet_key()
    EncryptedFileSecretStore(path=path, key=key).set_secret("secret:1", "sk-value")
    reopened = EncryptedFileSecretStore(path=path, key=key)
    assert reopened.get_secret("secret:1") == "sk-value"


def test_encrypted_file_store_wrong_key_returns_none_not_a_crash(tmp_path):
    from app.secrets_store import EncryptedFileSecretStore

    path = tmp_path / "secrets.enc.json"
    EncryptedFileSecretStore(path=path, key=_fernet_key()).set_secret("secret:1", "sk-value")
    wrong_key_store = EncryptedFileSecretStore(path=path, key=_fernet_key())
    assert wrong_key_store.get_secret("secret:1") is None


def test_encrypted_file_store_delete_removes_the_entry(tmp_path):
    from app.secrets_store import EncryptedFileSecretStore

    path = tmp_path / "secrets.enc.json"
    key = _fernet_key()
    store = EncryptedFileSecretStore(path=path, key=key)
    store.set_secret("secret:1", "sk-value")
    store.delete_secret("secret:1")
    assert store.get_secret("secret:1") is None


def test_encrypted_file_store_get_of_unknown_ref_is_none(tmp_path):
    from app.secrets_store import EncryptedFileSecretStore

    store = EncryptedFileSecretStore(path=tmp_path / "secrets.enc.json", key=_fernet_key())
    assert store.get_secret("secret:does-not-exist") is None


def test_encrypted_file_store_requires_a_key():
    from app.secrets_store import EncryptedFileSecretStore

    try:
        EncryptedFileSecretStore(path=None, key=None)
        assert False, "expected RuntimeError when no key is configured"
    except RuntimeError:
        pass


def test_get_secret_store_selects_keyring_backend_when_not_hosted(monkeypatch):
    import app.secrets_store as secrets_store_module
    from app.config import settings

    monkeypatch.setattr(secrets_store_module, "_store", None)
    monkeypatch.setattr(settings, "hosted_mode", False)

    store = secrets_store_module.get_secret_store()
    assert isinstance(store, secrets_store_module.KeyringSecretStore)


def test_get_secret_store_selects_encrypted_file_backend_when_hosted(monkeypatch, tmp_path):
    import app.secrets_store as secrets_store_module
    from app.config import settings

    monkeypatch.setattr(secrets_store_module, "_store", None)
    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "secret_encryption_key", _fernet_key())
    monkeypatch.setattr(settings, "secrets_file_path", tmp_path / "secrets.enc.json")

    store = secrets_store_module.get_secret_store()
    assert isinstance(store, secrets_store_module.EncryptedFileSecretStore)

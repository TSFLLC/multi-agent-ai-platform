"""Local secure secret storage — MA2, Section 20.1/20.2/24.4 #19.

Provider credentials (e.g. an OpenRouter API key) are never stored by
value in an ordinary domain table. ``secret_references.secret_store_ref``
is an opaque pointer into whatever this module resolves to at runtime —
local development uses the OS-native credential store (Windows Credential
Manager / macOS Keychain / Secret Service on Linux) via the ``keyring``
package; the automated test suite uses an in-memory fake so tests never
touch, pollute, or require interactive access to the real OS keychain;
a hosted deployment (MA7.7B, ``settings.hosted_mode``) uses
``EncryptedFileSecretStore`` instead, since a container has no OS
keychain.

This is the "smallest appropriate secret-store adapter" the Owner asked
for — a two-method interface small enough to swap backends behind
without touching any caller, not a new vault product.
"""

import json
import logging
from pathlib import Path
from typing import Optional, Protocol

from app.config import settings

logger = logging.getLogger("app.secrets_store")

SERVICE_NAME = "multi-agent-platform"


class SecretStore(Protocol):
    def set_secret(self, ref: str, value: str) -> None: ...

    def get_secret(self, ref: str) -> Optional[str]: ...

    def delete_secret(self, ref: str) -> None: ...


class KeyringSecretStore:
    """Local-dev backend — OS-native credential store via ``keyring``."""

    def set_secret(self, ref: str, value: str) -> None:
        import keyring

        keyring.set_password(SERVICE_NAME, ref, value)

    def get_secret(self, ref: str) -> Optional[str]:
        import keyring

        return keyring.get_password(SERVICE_NAME, ref)

    def delete_secret(self, ref: str) -> None:
        import keyring
        import keyring.errors

        try:
            keyring.delete_password(SERVICE_NAME, ref)
        except keyring.errors.PasswordDeleteError:
            pass


class EncryptedFileSecretStore:
    """Hosted-deployment backend (MA7.7B) — a container has no OS
    keychain for ``KeyringSecretStore`` to use, so values are instead
    stored Fernet-encrypted in a single JSON file on the persistent
    volume (``settings.secrets_file_path``).

    The encryption key itself is never persisted by this application — it
    must be supplied externally via ``MAP_SECRET_ENCRYPTION_KEY`` (a
    Railway secret), the same reference-not-value posture
    ``secret_references`` already uses one layer up (Section 20.1/20.2):
    losing the key file without the key is safe (unreadable, not
    silently plaintext); losing the key without the file is just an
    empty store. Construction fails fast (via ``app.config.Settings``'s
    own validation) if the key is missing or malformed, rather than
    this class discovering that lazily on first use.

    Writes are atomic (write to a temp file, then ``os.replace``) so a
    process killed mid-write can never leave a half-written, corrupt
    store — the previous version can only be the whole old file or the
    whole new one.
    """

    def __init__(self, path: Optional[Path] = None, key: Optional[str] = None) -> None:
        from cryptography.fernet import Fernet

        self._path = path or settings.secrets_file_path
        key_value = key if key is not None else settings.secret_encryption_key
        if not key_value:
            raise RuntimeError(
                "EncryptedFileSecretStore requires a Fernet key (MAP_SECRET_ENCRYPTION_KEY) "
                "— none was configured."
            )
        self._fernet = Fernet(key_value.encode("utf-8"))

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        raw = self._path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        return json.loads(raw)

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        tmp_path.replace(self._path)  # atomic on the same filesystem (POSIX and Windows alike)

    def set_secret(self, ref: str, value: str) -> None:
        data = self._load()
        data[ref] = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        self._save(data)

    def get_secret(self, ref: str) -> Optional[str]:
        data = self._load()
        token = data.get(ref)
        if token is None:
            return None
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except Exception:  # noqa: BLE001 — Fernet.decrypt() raises several distinct exception
            # types for "this token doesn't decrypt with this key" (wrong key, corrupted
            # token, wrong format); all of them mean the same thing here. Wrong/rotated
            # key, or a corrupted entry — treated the same
            # as "not configured," never as a crash: callers already
            # handle a missing provider credential as an ordinary,
            # expected state (app.services.secret_service).
            logger.warning("encrypted_secret_undecryptable ref=%s", ref)
            return None

    def delete_secret(self, ref: str) -> None:
        data = self._load()
        if ref in data:
            del data[ref]
            self._save(data)


class InMemorySecretStore:
    """Test-only backend — never touches the real OS keychain or disk."""

    def __init__(self) -> None:
        self._values: dict = {}

    def set_secret(self, ref: str, value: str) -> None:
        self._values[ref] = value

    def get_secret(self, ref: str) -> Optional[str]:
        return self._values.get(ref)

    def delete_secret(self, ref: str) -> None:
        self._values.pop(ref, None)


_store: Optional[SecretStore] = None


def _build_default_store() -> SecretStore:
    if settings.hosted_mode:
        return EncryptedFileSecretStore()
    return KeyringSecretStore()


def get_secret_store() -> SecretStore:
    # Lazy, not module-import-time: constructing EncryptedFileSecretStore
    # touches settings.secret_encryption_key, and constructing
    # KeyringSecretStore's real backend only happens inside its methods
    # anyway — neither needs to happen for a process that never calls
    # this (e.g. a hosted process shouldn't need `keyring` importable at
    # all, nor should a local process need a Fernet key configured).
    global _store
    if _store is None:
        _store = _build_default_store()
    return _store


def set_secret_store(store: SecretStore) -> None:
    """Test hook — swap in an InMemorySecretStore for the duration of a
    test. Also the mechanism a hosted process could use to inject a
    pre-built EncryptedFileSecretStore if it ever needed to (not
    currently exercised outside tests)."""
    global _store
    _store = store

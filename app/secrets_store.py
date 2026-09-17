"""Local secure secret storage — MA2, Section 20.1/20.2/24.4 #19.

Provider credentials (e.g. an OpenRouter API key) are never stored by
value in an ordinary domain table. ``secret_references.secret_store_ref``
is an opaque pointer into whatever this module resolves to at runtime —
production uses the OS-native credential store (Windows Credential
Manager / macOS Keychain / Secret Service on Linux) via the ``keyring``
package; the automated test suite uses an in-memory fake so tests never
touch, pollute, or require interactive access to the real OS keychain.

This is the "smallest appropriate local secret-store adapter" the Owner
asked for — no custom encryption, no vault file format to get wrong,
just OS-native storage behind a two-method interface small enough to
swap out later without touching any caller.
"""

import logging
from typing import Optional, Protocol

logger = logging.getLogger("app.secrets_store")

SERVICE_NAME = "multi-agent-platform"


class SecretStore(Protocol):
    def set_secret(self, ref: str, value: str) -> None: ...

    def get_secret(self, ref: str) -> Optional[str]: ...

    def delete_secret(self, ref: str) -> None: ...


class KeyringSecretStore:
    """Production backend — OS-native credential store via ``keyring``."""

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


class InMemorySecretStore:
    """Test-only backend — never touches the real OS keychain."""

    def __init__(self) -> None:
        self._values: dict = {}

    def set_secret(self, ref: str, value: str) -> None:
        self._values[ref] = value

    def get_secret(self, ref: str) -> Optional[str]:
        return self._values.get(ref)

    def delete_secret(self, ref: str) -> None:
        self._values.pop(ref, None)


_store: SecretStore = KeyringSecretStore()


def get_secret_store() -> SecretStore:
    return _store


def set_secret_store(store: SecretStore) -> None:
    """Test hook — swap in an InMemorySecretStore for the duration of a test."""
    global _store
    _store = store

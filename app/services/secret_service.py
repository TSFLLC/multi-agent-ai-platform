"""Secret reference service — MA2.

Bridges the frozen ``secret_references`` contract (Section 24.4 #19) to
the local secret store (app.secrets_store). The database never sees the
raw value: it only ever gets an opaque ``secret_store_ref`` string, and
that string is meaningless without the secret store behind it.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.governance import SecretReference
from app.secrets_store import get_secret_store


class SecretService:
    def __init__(self, db: Session):
        self.db = db

    def store_secret(
        self,
        *,
        project_id: str,
        provider_id: Optional[str],
        name: str,
        value: str,
        created_by: Optional[str] = None,
    ) -> SecretReference:
        """Writes ``value`` to the secret store under a fresh opaque
        reference, then records only that reference in the database."""
        ref = f"secret:{uuid.uuid4()}"
        get_secret_store().set_secret(ref, value)

        row = SecretReference(
            project_id=project_id,
            provider_id=provider_id,
            name=name,
            secret_store_ref=ref,
            created_by=created_by,
            created_at=self._next_created_at(provider_id),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _next_created_at(self, provider_id: Optional[str]) -> datetime:
        """Guarantees a strictly-increasing created_at per provider,
        regardless of wall-clock resolution — get_current_provider_api_key
        below orders by created_at DESC to find "the current" credential,
        and a tie (observed in practice: this system's clock can return
        the same value for two calls milliseconds apart) would make "most
        recent" ambiguous rather than wrong-but-deterministic."""
        now = datetime.now(timezone.utc)
        if provider_id is None:
            return now
        latest = (
            self.db.execute(
                select(SecretReference.created_at)
                .where(SecretReference.provider_id == provider_id)
                .order_by(SecretReference.created_at.desc())
            )
            .scalars()
            .first()
        )
        if latest is not None:
            # SQLite has no native timezone-aware storage — a DateTime
            # (timezone=True) column round-trips as a naive datetime here
            # even though it was written as UTC-aware. Without
            # reattaching tzinfo, comparing against `now` (aware) raises
            # TypeError rather than comparing correctly.
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            if latest >= now:
                return latest + timedelta(microseconds=1)
        return now

    def resolve_secret(self, secret_reference_id: str) -> Optional[str]:
        """Returns the raw secret value for a secret_references row, or
        None if the row or the underlying secret is missing. Callers must
        never log, persist, or echo the return value."""
        row = self.db.get(SecretReference, secret_reference_id)
        if row is None:
            return None
        return get_secret_store().get_secret(row.secret_store_ref)

    def rotate_secret(self, secret_reference_id: str, new_value: str) -> Optional[SecretReference]:
        row = self.db.get(SecretReference, secret_reference_id)
        if row is None:
            return None
        get_secret_store().set_secret(row.secret_store_ref, new_value)
        row.rotated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(row)
        return row

    def get_current_provider_api_key(self, provider_id: str) -> Optional[str]:
        """The credential lookup is one-directional (secret_references ->
        providers, never the reverse — see app.models.providers.Provider's
        docstring on why): the most recently created/rotated
        secret_references row for this provider is "the" current
        credential. Returns None if none is configured — callers must
        treat that as "no API key configured," not raise."""
        row = (
            self.db.execute(
                select(SecretReference)
                .where(SecretReference.provider_id == provider_id)
                .order_by(SecretReference.created_at.desc())
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        return get_secret_store().get_secret(row.secret_store_ref)

    def delete_secret(self, secret_reference_id: str) -> bool:
        row = self.db.get(SecretReference, secret_reference_id)
        if row is None:
            return False
        get_secret_store().delete_secret(row.secret_store_ref)
        self.db.delete(row)
        self.db.commit()
        return True

"""Audit Service — Section I, 20.5.

Security/compliance event writer, structurally and physically separate
from the Flight Recorder (different table, different repository, never
merged). New in MA1 — Section 11's component table doesn't name a
dedicated "Audit Service" because v1.0 treated audit writes as an
implicit side effect of other services; this makes it an explicit,
reusable component instead.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.observability import AuditEvent
from app.repositories.audit_event_repository import AuditEventRepository


class AuditService:
    def __init__(self, db: Session):
        self.db = db
        self._repo = AuditEventRepository()

    def record(
        self,
        *,
        org_id: str,
        event_type: str,
        actor_user_id: Optional[str] = None,
        target_ref: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> AuditEvent:
        return self._repo.record(
            self.db,
            org_id=org_id,
            event_type=event_type,
            actor_user_id=actor_user_id,
            target_ref=target_ref,
            detail=detail,
        )

    def list_for_org(self, *, org_id: str, limit: int = 100) -> List[AuditEvent]:
        return self._repo.list_for_org(self.db, org_id=org_id, limit=limit)

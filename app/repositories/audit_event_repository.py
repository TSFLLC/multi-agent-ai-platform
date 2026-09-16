"""AuditEventRepository — the security/compliance log, kept structurally
separate from the Flight Recorder (Section I, 20.5, 24.4 #4)."""

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.observability import AuditEvent


class AuditEventRepository:
    def record(
        self,
        db: Session,
        *,
        org_id: str,
        event_type: str,
        actor_user_id: Optional[str] = None,
        target_ref: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> AuditEvent:
        event = AuditEvent(
            org_id=org_id,
            event_type=event_type,
            actor_user_id=actor_user_id,
            target_ref=target_ref,
            detail=detail,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    def list_for_org(self, db: Session, *, org_id: str, limit: int = 100) -> List[AuditEvent]:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.org_id == org_id)
            .order_by(AuditEvent.occurred_at.desc())
            .limit(limit)
        )
        return list(db.execute(stmt).scalars().all())

"""IdempotencyKeyRepository — Section G, 24.4 #11, 25.4/28.3.

One durable mechanism for both API-request dedup (``Idempotency-Key``
header) and side-effecting-tool dedup. ``try_begin`` is the compare-and-
swap: a brand-new key claims the row; a key already ``COMPLETED`` returns
the stored result instead of re-running anything; a key already
``IN_PROGRESS`` signals a genuine concurrent conflict; a key that
previously ``FAILED`` is safe to reclaim, since the guarded operation
never actually completed.
"""

from enum import Enum
from typing import Any, Dict, NamedTuple, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.enums import IdempotencyScope, IdempotencyStatus
from app.models.execution import IdempotencyKey


class BeginOutcome(str, Enum):
    STARTED = "started"  # caller should proceed and later call complete()/fail()
    ALREADY_COMPLETED = "already_completed"  # return .existing.result_ref, do not re-run
    IN_PROGRESS_ELSEWHERE = "in_progress_elsewhere"  # genuine concurrent conflict, 409


class BeginResult(NamedTuple):
    outcome: BeginOutcome
    key_row: IdempotencyKey


class IdempotencyKeyRepository:
    def try_begin(self, db: Session, *, key: str, scope: IdempotencyScope, resource_type: str) -> BeginResult:
        row = IdempotencyKey(
            key=key, scope=scope, resource_type=resource_type, status=IdempotencyStatus.IN_PROGRESS
        )
        db.add(row)
        try:
            db.commit()
            db.refresh(row)
            return BeginResult(BeginOutcome.STARTED, row)
        except IntegrityError:
            db.rollback()

        existing = db.execute(select(IdempotencyKey).where(IdempotencyKey.key == key)).scalar_one()
        if existing.status == IdempotencyStatus.COMPLETED:
            return BeginResult(BeginOutcome.ALREADY_COMPLETED, existing)
        if existing.status == IdempotencyStatus.IN_PROGRESS:
            return BeginResult(BeginOutcome.IN_PROGRESS_ELSEWHERE, existing)

        # FAILED — safe to reclaim; the guarded operation never completed.
        existing.status = IdempotencyStatus.IN_PROGRESS
        existing.result_ref = None
        db.commit()
        db.refresh(existing)
        return BeginResult(BeginOutcome.STARTED, existing)

    def complete(self, db: Session, *, key: str, result_ref: Optional[Dict[str, Any]] = None) -> None:
        row = db.execute(select(IdempotencyKey).where(IdempotencyKey.key == key)).scalar_one()
        row.status = IdempotencyStatus.COMPLETED
        row.result_ref = result_ref
        db.commit()

    def fail(self, db: Session, *, key: str) -> None:
        row = db.execute(select(IdempotencyKey).where(IdempotencyKey.key == key)).scalar_one()
        row.status = IdempotencyStatus.FAILED
        db.commit()

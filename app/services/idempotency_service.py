"""Idempotency Service — Section G, 25.4, 28.3.

Wraps IdempotencyKeyRepository with the app-level error contract: a
genuine concurrent conflict on the same key raises ``IdempotencyConflictError``
(-> 409) rather than the caller having to branch on a raw enum.

Usage pattern for a state-changing endpoint::

    begin = idempotency.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="...")
    if begin.outcome is BeginOutcome.ALREADY_COMPLETED:
        return begin.key_row.result_ref
    try:
        result = do_the_actual_work()
        idempotency.complete(key, result_ref=result)
        return result
    except Exception:
        idempotency.fail(key)
        raise
"""

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.db.enums import IdempotencyScope
from app.errors import IdempotencyConflictError
from app.repositories.idempotency_repository import BeginOutcome, BeginResult, IdempotencyKeyRepository


class IdempotencyService:
    def __init__(self, db: Session):
        self.db = db
        self._repo = IdempotencyKeyRepository()

    def begin(self, key: str, *, scope: IdempotencyScope, resource_type: str) -> BeginResult:
        result = self._repo.try_begin(self.db, key=key, scope=scope, resource_type=resource_type)
        if result.outcome == BeginOutcome.IN_PROGRESS_ELSEWHERE:
            raise IdempotencyConflictError(
                f"Idempotency-Key {key!r} is already being processed by another in-flight request.",
                detail={"key": key, "resource_type": resource_type},
            )
        return result

    def complete(self, key: str, *, result_ref: Optional[Dict[str, Any]] = None) -> None:
        self._repo.complete(self.db, key=key, result_ref=result_ref)

    def fail(self, key: str) -> None:
        self._repo.fail(self.db, key=key)

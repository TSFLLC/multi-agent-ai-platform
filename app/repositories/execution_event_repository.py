"""ExecutionEventRepository — the Flight Recorder's real write/read path.

Section H: append-only, monotonically ordered per task_run_id, safe under
concurrent writers, resumable via ``after_sequence`` (the SSE
``last_event_id`` contract, Section 25.3). Never accepts a raw
chain-of-thought field (Section 22.5) — callers pass a structured
``decision_summary``.
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.observability import ExecutionEvent

_MAX_SEQUENCE_RETRIES = 5


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionEventRepository:
    def record(
        self,
        db: Session,
        *,
        task_id: str,
        task_run_id: str,
        event_type: str,
        **fields,
    ) -> ExecutionEvent:
        """Assigns the next sequence_number for this task_run_id and
        inserts the event, retrying if a concurrent writer won the same
        number first (uq_execution_events_task_run_id_sequence_number)."""
        last_error: Optional[IntegrityError] = None
        for _ in range(_MAX_SEQUENCE_RETRIES):
            next_seq = (
                db.execute(
                    select(func.coalesce(func.max(ExecutionEvent.sequence_number), 0)).where(
                        ExecutionEvent.task_run_id == task_run_id
                    )
                ).scalar_one()
                + 1
            )
            event = ExecutionEvent(
                task_id=task_id,
                task_run_id=task_run_id,
                sequence_number=next_seq,
                event_type=event_type,
                occurred_at=utcnow(),
                **fields,
            )
            db.add(event)
            try:
                db.commit()
                db.refresh(event)
                return event
            except IntegrityError as exc:
                db.rollback()
                last_error = exc
        raise RuntimeError(
            f"Could not assign a unique sequence_number for task_run_id={task_run_id} "
            f"after {_MAX_SEQUENCE_RETRIES} attempts"
        ) from last_error

    def list_for_task_run(
        self, db: Session, *, task_run_id: str, after_sequence: Optional[int] = None
    ) -> List[ExecutionEvent]:
        stmt = select(ExecutionEvent).where(ExecutionEvent.task_run_id == task_run_id)
        if after_sequence is not None:
            stmt = stmt.where(ExecutionEvent.sequence_number > after_sequence)
        stmt = stmt.order_by(ExecutionEvent.sequence_number)
        return list(db.execute(stmt).scalars().all())

    def latest_sequence(self, db: Session, *, task_run_id: str) -> int:
        return db.execute(
            select(func.coalesce(func.max(ExecutionEvent.sequence_number), 0)).where(
                ExecutionEvent.task_run_id == task_run_id
            )
        ).scalar_one()

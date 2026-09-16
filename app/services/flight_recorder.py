"""Flight Recorder writer/reader — Section 11, 22, H.

Append-only; never stores hidden chain-of-thought (Section 22.5), only
structured fields and a ``decision_summary`` string. This is now a real
implementation over ``ExecutionEventRepository`` — MA0 only established
the table/constraint; MA1 makes the atomic sequence-number assignment and
resumable retrieval real.
"""

from typing import Any, List, Optional

from sqlalchemy.orm import Session

from app.models.observability import ExecutionEvent
from app.repositories.execution_event_repository import ExecutionEventRepository


class FlightRecorderService:
    def __init__(self, db: Session):
        self.db = db
        self._repo = ExecutionEventRepository()

    def record(self, *, task_id: str, task_run_id: str, event_type: str, **fields: Any) -> ExecutionEvent:
        return self._repo.record(
            self.db, task_id=task_id, task_run_id=task_run_id, event_type=event_type, **fields
        )

    def list_for_task_run(
        self, *, task_run_id: str, after_sequence: Optional[int] = None
    ) -> List[ExecutionEvent]:
        return self._repo.list_for_task_run(self.db, task_run_id=task_run_id, after_sequence=after_sequence)

    def latest_sequence(self, *, task_run_id: str) -> int:
        return self._repo.latest_sequence(self.db, task_run_id=task_run_id)

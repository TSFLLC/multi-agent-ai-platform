"""Flight Recorder writer/reader — Section 11, 22. Append-only; never
stores hidden chain-of-thought (Section 22.5), only structured
decision_summary text. Real implementation: MA3.

The ``execution_events`` table and its monotonic-per-task_run
sequence_number constraint already exist (app.models.observability); this
service is the write-path that assigns sequence numbers atomically and the
read-path that powers SSE resumability (Section 25.3).
"""

from app.services.base import BaseService


class FlightRecorderService(BaseService):
    def record(self, *args, **kwargs):
        raise NotImplementedError("FlightRecorderService lands in MA3")

    def next_sequence_number(self, task_run_id: str) -> int:
        """Must be assigned within the same short transaction as the event
        insert to preserve the uq_execution_events_task_run_id_sequence_number
        constraint under concurrent writers."""
        raise NotImplementedError("FlightRecorderService lands in MA3")

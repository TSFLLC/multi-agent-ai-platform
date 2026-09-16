from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ExecutionEventRead(BaseModel):
    """Section 22.2 — never carries hidden chain-of-thought, only structured
    decision_summary text."""

    id: str
    occurred_at: datetime
    sequence_number: int
    task_run_id: str
    event_type: str
    decision_summary: Optional[str] = None


class AuditEventRead(BaseModel):
    id: str
    org_id: str
    event_type: str
    target_ref: Optional[str] = None
    occurred_at: datetime

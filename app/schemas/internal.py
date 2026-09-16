"""MA1 operator/scaffolding contracts — not part of the frozen product API
surface from Section 25.2/25.5. Exists solely to prove the worker +
idempotency foundations end-to-end without real Agent execution."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.db.enums import JobQueueStatus
from app.schemas.common import ORMModel


class InternalTestJobCreate(BaseModel):
    note: Optional[str] = None


class InternalTestJobRead(ORMModel):
    id: str
    status: JobQueueStatus
    lease_owner: Optional[str] = None
    fencing_token: int
    attempt_count: int
    created_at: datetime

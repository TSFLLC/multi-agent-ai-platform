"""Shared Pydantic building blocks for API contracts."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TimestampedRead(ORMModel):
    id: str
    created_at: datetime


class ErrorResponse(BaseModel):
    detail: str


class IdempotentRequest(BaseModel):
    """Not a request body field — see the ``Idempotency-Key`` header used by
    state-changing POSTs (Section 25.4). Present here only as shared
    documentation; the header itself is declared per-endpoint.
    """


class FingerprintMismatch(BaseModel):
    """409 body shape for ``POST /approvals/{id}/resolve`` — Section 25.5."""

    detail: str = "fingerprint_mismatch"
    expected_action_fingerprint: Optional[str] = None

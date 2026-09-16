"""Global Flight Recorder query boundary — Section 25.2, 22."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.schemas.events import ExecutionEventRead

router = APIRouter(tags=["events"])


@router.get("/events", response_model=List[ExecutionEventRead])
def query_events(db: Session = Depends(get_db), task_run_id: Optional[str] = Query(default=None)):
    not_implemented()


@router.get("/events/stream")
def stream_events(last_event_id: Optional[int] = None):
    """SSE; resumable via last_event_id against execution_events.sequence_number."""
    not_implemented()

"""Task / Task Run / Agent Run resource boundary — Section 25.2, 25.3, 25.5."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_session_factory, not_implemented
from app.schemas.events import ExecutionEventRead
from app.schemas.tasks import AgentRunAttemptRead, AgentRunRead, TaskCreate, TaskRead, TaskRunRead
from app.services.flight_recorder import FlightRecorderService
from app.sse import stream_task_run_events as _sse_stream

router = APIRouter(tags=["tasks"])


@router.post("/tasks", response_model=TaskRead, status_code=201)
def create_task(
    body: TaskCreate,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    not_implemented()


@router.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(task_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.post("/tasks/{task_id}/runs", response_model=TaskRunRead, status_code=201)
def start_task_run(
    task_id: str,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    not_implemented()


@router.get("/tasks/{task_id}/runs", response_model=List[TaskRunRead])
def list_task_runs(task_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/tasks/{task_id}/runs/{run_id}", response_model=TaskRunRead)
def get_task_run(task_id: str, run_id: str, db: Session = Depends(get_db)):
    """Includes config_snapshot (Section 24.4 #9) inline."""
    not_implemented()


@router.post("/tasks/{task_id}/runs/{run_id}/cancel", response_model=TaskRunRead)
def cancel_task_run(task_id: str, run_id: str, db: Session = Depends(get_db)):
    """Returns immediately with CANCELLING status — cancellation is
    cooperative, not instant (Section 26.7)."""
    not_implemented()


@router.post("/tasks/{task_id}/runs/{run_id}/approve-continue", response_model=TaskRunRead)
def approve_continue_task_run(task_id: str, run_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/tasks/{task_id}/runs/{run_id}/events", response_model=List[ExecutionEventRead])
def get_task_run_events(
    task_id: str,
    run_id: str,
    db: Session = Depends(get_db),
    after_sequence: Optional[int] = None,
):
    """Queries execution_events (Section 24.4 #4), never audit_events."""
    recorder = FlightRecorderService(db)
    return recorder.list_for_task_run(task_run_id=run_id, after_sequence=after_sequence)


@router.get("/tasks/{task_id}/runs/{run_id}/stream")
def stream_task_run_events(
    task_id: str,
    run_id: str,
    last_event_id: Optional[int] = None,
    session_factory=Depends(get_session_factory),
):
    """SSE stream. Reconnection resumes from ``last_event_id`` against
    execution_events.sequence_number (Section 25.3)."""
    return StreamingResponse(
        _sse_stream(run_id, last_event_id=last_event_id, session_factory=session_factory),
        media_type="text/event-stream",
    )


@router.get("/agent-runs/{agent_run_id}", response_model=AgentRunRead)
def get_agent_run(agent_run_id: str, db: Session = Depends(get_db)):
    """Includes provider_model_snapshot inline (Section 25.5)."""
    not_implemented()


@router.post("/agent-runs/{agent_run_id}/stop", response_model=AgentRunRead)
def stop_agent_run(agent_run_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.post("/agent-runs/{agent_run_id}/retry", response_model=AgentRunRead)
def retry_agent_run(agent_run_id: str, db: Session = Depends(get_db)):
    """Creates a new agent_run_attempts row (Section 24.4 #10) — never
    mutates a prior attempt."""
    not_implemented()


@router.get("/agent-runs/{agent_run_id}/events", response_model=List[ExecutionEventRead])
def get_agent_run_events(agent_run_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/agent-runs/{agent_run_id}/artifacts")
def get_agent_run_artifacts(agent_run_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/agent-runs/{agent_run_id}/attempts", response_model=List[AgentRunAttemptRead])
def get_agent_run_attempts(agent_run_id: str, db: Session = Depends(get_db)):
    not_implemented()

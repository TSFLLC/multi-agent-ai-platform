"""Task / Task Run / Agent Run resource boundary — Section 25.2, 25.3, 25.5.

Every real endpoint requires authentication and project authorization
(app.authz), resolved through the Task -> Task Run -> Agent Run chain
since only ``tasks.project_id`` carries the project directly (Section
24.2) — mirrors the pattern already established in app.api.routers.agents.
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_session_factory, not_implemented
from app.auth import get_current_user
from app.authz import ProjectAction, check_project_access, require_project_access
from app.db.enums import IdempotencyScope
from app.errors import NotFoundError
from app.models.artifacts_eval import Artifact
from app.models.execution import ModelCall
from app.models.identity import User
from app.models.tasks import AgentRun, AgentRunAttempt, Task, TaskRun
from app.schemas.artifacts import ArtifactRead
from app.schemas.events import ExecutionEventRead
from app.schemas.reviews import (
    AgentReviewRead,
    ReviewedTaskRunCreate,
    ReviewedTaskRunSummary,
    TaskRunUsageTotal,
)
from app.schemas.tasks import (
    AgentRunAttemptRead,
    AgentRunRead,
    TaskCreate,
    TaskRead,
    TaskRunCreate,
    TaskRunRead,
)
from app.schemas.usage import ModelCallRead
from app.services.flight_recorder import FlightRecorderService
from app.services.idempotency_service import BeginOutcome, IdempotencyService
from app.services.review_orchestration_service import get_review_summary
from app.services.task_service import TaskService
from app.sse import stream_task_run_events as _sse_stream

router = APIRouter(tags=["tasks"])


# -- authorization helpers: resolve the owning project through the chain -----


def _get_task_or_404(db: Session, task_id: str) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise NotFoundError(f"Task {task_id} not found.")
    return task


def _get_task_run_or_404(db: Session, task_id: str, run_id: str) -> TaskRun:
    task_run = db.get(TaskRun, run_id)
    if task_run is None or task_run.task_id != task_id:
        raise NotFoundError(f"Task Run {run_id} not found for task {task_id}.")
    return task_run


def _get_agent_run_or_404(db: Session, agent_run_id: str) -> AgentRun:
    agent_run = db.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise NotFoundError(f"Agent Run {agent_run_id} not found.")
    return agent_run


def _project_id_for_agent_run(db: Session, agent_run: AgentRun) -> str:
    task_run = db.get(TaskRun, agent_run.task_run_id)
    if task_run is None:
        raise NotFoundError(f"Task Run {agent_run.task_run_id} not found.")
    task = db.get(Task, task_run.task_id)
    if task is None:
        raise NotFoundError(f"Task {task_run.task_id} not found.")
    return task.project_id


def _require_task_access(db: Session, user: User, task: Task, action: ProjectAction) -> None:
    check_project_access(db, user=user, project_id=task.project_id, action=action)


# -- Tasks -------------------------------------------------------------------


@router.post("/tasks", response_model=TaskRead, status_code=201)
def create_task(
    body: TaskCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    check_project_access(db, user=user, project_id=body.project_id, action=ProjectAction.MODIFY)

    key = idempotency_key or f"auto:{uuid.uuid4()}"
    idempotency = IdempotencyService(db)
    begin = idempotency.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="task")
    if begin.outcome == BeginOutcome.ALREADY_COMPLETED:
        task_id = (begin.key_row.result_ref or {}).get("task_id")
        if task_id is None:
            raise NotFoundError(f"Idempotency key {key!r} completed with no recorded task_id.")
        return _get_task_or_404(db, task_id)

    try:
        task = TaskService(db).create_task(
            project_id=body.project_id,
            title=body.title,
            description=body.description,
            execution_mode=body.execution_mode,
            requirements=body.requirements,
            created_by=user.id,
        )
        idempotency.complete(key, result_ref={"task_id": task.id})
        return task
    except Exception:
        idempotency.fail(key)
        raise


@router.get("/tasks", response_model=List[TaskRead])
def list_tasks(
    db: Session = Depends(get_db),
    membership=Depends(require_project_access(ProjectAction.READ)),
):
    return TaskService(db).list_tasks(project_id=membership.project_id)


@router.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(task_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task = _get_task_or_404(db, task_id)
    _require_task_access(db, user, task, ProjectAction.READ)
    return task


# -- Task Runs -----------------------------------------------------------------


@router.post("/tasks/{task_id}/runs", response_model=TaskRunRead, status_code=201)
def start_task_run(
    task_id: str,
    body: TaskRunCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    task = _get_task_or_404(db, task_id)
    _require_task_access(db, user, task, ProjectAction.MODIFY)

    key = idempotency_key or f"auto:{uuid.uuid4()}"
    idempotency = IdempotencyService(db)
    begin = idempotency.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="task_run")
    if begin.outcome == BeginOutcome.ALREADY_COMPLETED:
        run_id = (begin.key_row.result_ref or {}).get("task_run_id")
        if run_id is None:
            raise NotFoundError(f"Idempotency key {key!r} completed with no recorded task_run_id.")
        return _get_task_run_or_404(db, task_id, run_id)

    try:
        task_run = TaskService(db).start_task_run(
            task_id=task_id, agent_version_id=body.agent_version_id, budget_id=body.budget_id
        )
        idempotency.complete(key, result_ref={"task_run_id": task_run.id})
        return task_run
    except Exception:
        idempotency.fail(key)
        raise


@router.post("/tasks/{task_id}/runs/reviewed", response_model=TaskRunRead, status_code=201)
def start_reviewed_task_run(
    task_id: str,
    body: ReviewedTaskRunCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """MA4: launches a BUILD_REVIEW Task Run (primary Agent -> reviewer
    Agent -> optional bounded repair). Primary/reviewer are independent
    Agent Versions and may use independent models -- Agent != Model."""
    task = _get_task_or_404(db, task_id)
    _require_task_access(db, user, task, ProjectAction.MODIFY)

    key = idempotency_key or f"auto:{uuid.uuid4()}"
    idempotency = IdempotencyService(db)
    begin = idempotency.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="task_run")
    if begin.outcome == BeginOutcome.ALREADY_COMPLETED:
        run_id = (begin.key_row.result_ref or {}).get("task_run_id")
        if run_id is None:
            raise NotFoundError(f"Idempotency key {key!r} completed with no recorded task_run_id.")
        return _get_task_run_or_404(db, task_id, run_id)

    try:
        task_run = TaskService(db).start_reviewed_task_run(
            task_id=task_id,
            primary_agent_version_id=body.primary_agent_version_id,
            reviewer_agent_version_id=body.reviewer_agent_version_id,
            max_repair_iterations=body.max_repair_iterations,
            review_instructions=body.review_instructions,
            budget_id=body.budget_id,
        )
        idempotency.complete(key, result_ref={"task_run_id": task_run.id})
        return task_run
    except Exception:
        idempotency.fail(key)
        raise


@router.get("/tasks/{task_id}/runs/{run_id}/review", response_model=ReviewedTaskRunSummary)
def get_task_run_review_summary(
    task_id: str, run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """MA4: one consolidated view of a BUILD_REVIEW Task Run -- its
    configuration, every Agent Run it produced (tagged primary/reviewer/
    repair), every candidate artifact and structured review, the final
    (accepted) artifact if any, and aggregate usage/cost across the whole
    cycle. Returns 404 for a Task Run that was never run in review mode
    (no config_snapshot review fields) exactly as it would for a missing
    Task Run -- there is nothing review-shaped to summarize either way."""
    task = _get_task_or_404(db, task_id)
    _require_task_access(db, user, task, ProjectAction.READ)
    task_run = _get_task_run_or_404(db, task_id, run_id)

    summary = get_review_summary(db, run_id)
    if summary is None or "primary_agent_version_id" not in (task_run.config_snapshot or {}):
        raise NotFoundError(f"Task Run {run_id} is not a reviewed (build_review) Task Run.")

    return ReviewedTaskRunSummary(
        task_run_id=summary.task_run.id,
        status=summary.task_run.status.value,
        outcome=summary.outcome,
        primary_agent_version_id=summary.primary_agent_version_id,
        reviewer_agent_version_id=summary.reviewer_agent_version_id,
        max_repair_iterations=summary.max_repair_iterations,
        agent_runs=[AgentRunRead.model_validate(r, from_attributes=True) for r in summary.agent_runs],
        reviews=[AgentReviewRead.model_validate(r, from_attributes=True) for r in summary.reviews],
        final_artifact=(
            ArtifactRead.model_validate(summary.final_artifact, from_attributes=True)
            if summary.final_artifact is not None
            else None
        ),
        usage=[TaskRunUsageTotal.model_validate(u, from_attributes=True) for u in summary.usage],
        total_cost=summary.total_cost,
    )


@router.get("/tasks/{task_id}/runs", response_model=List[TaskRunRead])
def list_task_runs(task_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task = _get_task_or_404(db, task_id)
    _require_task_access(db, user, task, ProjectAction.READ)
    return TaskService(db).list_task_runs(task_id=task_id)


@router.get("/tasks/{task_id}/runs/{run_id}", response_model=TaskRunRead)
def get_task_run(
    task_id: str, run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Includes config_snapshot (Section 24.4 #9) inline."""
    task = _get_task_or_404(db, task_id)
    _require_task_access(db, user, task, ProjectAction.READ)
    return _get_task_run_or_404(db, task_id, run_id)


@router.post("/tasks/{task_id}/runs/{run_id}/cancel", response_model=TaskRunRead)
def cancel_task_run(
    task_id: str, run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Returns immediately with CANCELLING status — cancellation is
    cooperative, not instant (Section 26.7)."""
    task = _get_task_or_404(db, task_id)
    _require_task_access(db, user, task, ProjectAction.MODIFY)
    _get_task_run_or_404(db, task_id, run_id)
    return TaskService(db).cancel_task_run(run_id, requested_by=user.id)


@router.post("/tasks/{task_id}/runs/{run_id}/approve-continue", response_model=TaskRunRead)
def approve_continue_task_run(task_id: str, run_id: str, db: Session = Depends(get_db)):
    """WAITING_FOR_APPROVAL / approval gates are not part of MA3's
    single-Agent-execution scope (no PolicyRule/Approval enforcement is
    wired into the execution path yet) — left as a contract stub."""
    not_implemented()


@router.get("/tasks/{task_id}/runs/{run_id}/events", response_model=List[ExecutionEventRead])
def get_task_run_events(
    task_id: str,
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    after_sequence: Optional[int] = None,
):
    """Queries execution_events (Section 24.4 #4), never audit_events."""
    task = _get_task_or_404(db, task_id)
    _require_task_access(db, user, task, ProjectAction.READ)
    _get_task_run_or_404(db, task_id, run_id)
    recorder = FlightRecorderService(db)
    return recorder.list_for_task_run(task_run_id=run_id, after_sequence=after_sequence)


@router.get("/tasks/{task_id}/runs/{run_id}/stream")
def stream_task_run_events(
    task_id: str,
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    last_event_id: Optional[int] = None,
    session_factory=Depends(get_session_factory),
):
    """SSE stream. Reconnection resumes from ``last_event_id`` against
    execution_events.sequence_number (Section 25.3)."""
    task = _get_task_or_404(db, task_id)
    _require_task_access(db, user, task, ProjectAction.READ)
    _get_task_run_or_404(db, task_id, run_id)
    return StreamingResponse(
        _sse_stream(run_id, last_event_id=last_event_id, session_factory=session_factory),
        media_type="text/event-stream",
    )


# -- Agent Runs ----------------------------------------------------------------


@router.get("/agent-runs/{agent_run_id}", response_model=AgentRunRead)
def get_agent_run(agent_run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Includes provider_model_snapshot_id inline (Section 25.5)."""
    agent_run = _get_agent_run_or_404(db, agent_run_id)
    check_project_access(
        db, user=user, project_id=_project_id_for_agent_run(db, agent_run), action=ProjectAction.READ
    )
    return agent_run


@router.post("/agent-runs/{agent_run_id}/stop", response_model=AgentRunRead)
def stop_agent_run(agent_run_id: str, db: Session = Depends(get_db)):
    """Not part of MA3's minimum API (Section 18 exposes cancellation at
    the Task Run level); left as a contract stub."""
    not_implemented()


@router.post("/agent-runs/{agent_run_id}/retry", response_model=AgentRunRead)
def retry_agent_run(agent_run_id: str, db: Session = Depends(get_db)):
    """Retries within an Agent Run's own attempt budget are automatic
    (app.services.execution_service, settings.max_agent_run_attempts) —
    a manual retry API is not part of MA3's minimum scope."""
    not_implemented()


@router.get("/agent-runs/{agent_run_id}/events", response_model=List[ExecutionEventRead])
def get_agent_run_events(
    agent_run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    agent_run = _get_agent_run_or_404(db, agent_run_id)
    check_project_access(
        db, user=user, project_id=_project_id_for_agent_run(db, agent_run), action=ProjectAction.READ
    )
    recorder = FlightRecorderService(db)
    events = recorder.list_for_task_run(task_run_id=agent_run.task_run_id)
    return [e for e in events if e.agent_run_id == agent_run_id]


@router.get("/agent-runs/{agent_run_id}/artifacts", response_model=List[ArtifactRead])
def get_agent_run_artifacts(
    agent_run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    agent_run = _get_agent_run_or_404(db, agent_run_id)
    check_project_access(
        db, user=user, project_id=_project_id_for_agent_run(db, agent_run), action=ProjectAction.READ
    )
    stmt = select(Artifact).where(Artifact.agent_run_id == agent_run_id).order_by(Artifact.created_at)
    return list(db.execute(stmt).scalars().all())


@router.get("/agent-runs/{agent_run_id}/attempts", response_model=List[AgentRunAttemptRead])
def get_agent_run_attempts(
    agent_run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    agent_run = _get_agent_run_or_404(db, agent_run_id)
    check_project_access(
        db, user=user, project_id=_project_id_for_agent_run(db, agent_run), action=ProjectAction.READ
    )
    stmt = (
        select(AgentRunAttempt)
        .where(AgentRunAttempt.agent_run_id == agent_run_id)
        .order_by(AgentRunAttempt.attempt_number)
    )
    return list(db.execute(stmt).scalars().all())


@router.get("/agent-runs/{agent_run_id}/usage", response_model=List[ModelCallRead])
def get_agent_run_usage(
    agent_run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Usage/cost accounting view (Section 24.4 #13 / 19) — one row per
    Model Call actually made for this Agent Run."""
    agent_run = _get_agent_run_or_404(db, agent_run_id)
    check_project_access(
        db, user=user, project_id=_project_id_for_agent_run(db, agent_run), action=ProjectAction.READ
    )
    stmt = select(ModelCall).where(ModelCall.agent_run_id == agent_run_id).order_by(ModelCall.started_at)
    return list(db.execute(stmt).scalars().all())


# -- Artifacts -----------------------------------------------------------------


@router.get("/artifacts/{artifact_id}/content", response_class=PlainTextResponse)
def get_artifact_content(
    artifact_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Raw artifact bytes, separate from the metadata list so a large
    artifact never has to be inlined into a JSON response (Section 24.4
    #18)."""
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise NotFoundError(f"Artifact {artifact_id} not found.")
    agent_run = _get_agent_run_or_404(db, artifact.agent_run_id)
    check_project_access(
        db, user=user, project_id=_project_id_for_agent_run(db, agent_run), action=ProjectAction.READ
    )
    with open(artifact.storage_ref, "r", encoding="utf-8") as f:
        content = f.read()
    return PlainTextResponse(content=content, media_type=artifact.mime_type or "text/plain")

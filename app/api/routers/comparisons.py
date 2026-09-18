"""Comparison resource boundary — Section 25.2, 25.5, 18. MA5: real Parallel
Comparison orchestration on top of MA3's execution engine and MA4's
Flight-Recorder-anchored patterns -- no second inference pipeline, no
automatic winner selection (Section 18.3 Principle 2/4: the human's
selection is always a manual action).

Every real endpoint requires authentication and project authorization
(app.authz), resolved through the comparison's own bookkeeping Task Run ->
Task chain, exactly like app.api.routers.tasks resolves it through
Task Run -> Task for Agent Runs.
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.authz import ProjectAction, check_project_access, require_project_access
from app.db.enums import IdempotencyScope
from app.errors import NotFoundError
from app.models.artifacts_eval import ComparisonRun
from app.models.identity import User
from app.models.tasks import Task, TaskRun
from app.schemas.comparisons import (
    ComparisonCandidateEvaluationResult,
    ComparisonCandidateRead,
    ComparisonEvaluationRequest,
    ComparisonEvaluationResponse,
    ComparisonReviewConfig,
    ComparisonRunCreate,
    ComparisonRunRead,
    SelectWinnerRequest,
)
from app.services.comparison_service import ComparisonService, get_comparison_detail
from app.services.evaluation_execution_service import EvaluationExecutionService
from app.services.idempotency_service import BeginOutcome, IdempotencyService

router = APIRouter(tags=["comparisons"])


# -- authorization helpers: resolve the owning project through the chain -----


def _get_comparison_or_404(db: Session, comparison_id: str) -> ComparisonRun:
    comparison = db.get(ComparisonRun, comparison_id)
    if comparison is None:
        raise NotFoundError(f"Comparison {comparison_id} not found.")
    return comparison


def _get_task_or_404(db: Session, task_id: str) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise NotFoundError(f"Task {task_id} not found.")
    return task


def _project_id_for_comparison(db: Session, comparison: ComparisonRun) -> str:
    task_run = db.get(TaskRun, comparison.task_run_id)
    if task_run is None:
        raise NotFoundError(f"Task Run {comparison.task_run_id} not found.")
    return _get_task_or_404(db, task_run.task_id).project_id


def _require_comparison_access(
    db: Session, user: User, comparison: ComparisonRun, action: ProjectAction
) -> None:
    check_project_access(db, user=user, project_id=_project_id_for_comparison(db, comparison), action=action)


# -- response shaping ----------------------------------------------------------


def _read(db: Session, comparison_id: str) -> ComparisonRunRead:
    detail = get_comparison_detail(db, comparison_id)
    comparison = detail.comparison
    bookkeeping_run = db.get(TaskRun, comparison.task_run_id)
    if bookkeeping_run is None:
        raise NotFoundError(f"Task Run {comparison.task_run_id} not found.")

    candidates = [
        ComparisonCandidateRead(
            id=c.candidate.id,
            label=c.candidate.label,
            agent_version_id=c.candidate.agent_version_id,
            model_policy_override=c.candidate.model_policy_override_json,
            review=(
                ComparisonReviewConfig(**c.candidate.review_config_json)
                if c.candidate.review_config_json
                else None
            ),
            task_run_id=c.candidate.task_run_id,
            agent_run_id=c.candidate.agent_run_id,
            status=c.status,
            model_id=c.model_id,
            provider_id=c.provider_id,
            artifact_id=c.artifact.id if c.artifact else None,
            artifact_hash=c.artifact.content_hash if c.artifact else None,
            tokens_in=c.usage.tokens_in,
            tokens_out=c.usage.tokens_out,
            cost_amount=c.usage.cost_amount,
            latency_ms=c.usage.latency_ms,
            rank=c.candidate.rank,
            is_winner=c.candidate.is_winner,
            created_at=c.candidate.created_at,
        )
        for c in detail.candidates
    ]

    return ComparisonRunRead(
        id=comparison.id,
        task_id=bookkeeping_run.task_id,
        task_run_id=comparison.task_run_id,
        status=comparison.status,
        phase=detail.phase,
        winner_agent_run_id=comparison.winner_agent_run_id,
        winner_artifact_id=comparison.winner_artifact_id,
        winner_artifact_hash=comparison.winner_artifact_hash,
        cancellation_requested_at=comparison.cancellation_requested_at,
        completed_at=comparison.completed_at,
        created_at=comparison.created_at,
        candidates=candidates,
        total_tokens_in=detail.total_tokens_in,
        total_tokens_out=detail.total_tokens_out,
        total_cost=detail.total_cost,
    )


# -- endpoints -------------------------------------------------------------------


@router.get("/comparisons", response_model=List[ComparisonRunRead])
def list_comparisons(
    db: Session = Depends(get_db),
    membership=Depends(require_project_access(ProjectAction.READ)),
):
    """MA5-UI addition: lets the operator console list and reopen
    previous comparisons (Section 25.2 already exposes GET /tasks and GET
    /agents the same project-scoped-list way — this fills the equivalent
    gap for comparisons, which MA5 never needed since every comparison was
    reached by an id the caller already had). Newest first; no new state,
    purely a read over the existing ComparisonRun/TaskRun/Task chain
    ``_read`` already resolves for the single-comparison GET."""
    stmt = (
        select(ComparisonRun.id)
        .join(TaskRun, TaskRun.id == ComparisonRun.task_run_id)
        .join(Task, Task.id == TaskRun.task_id)
        .where(Task.project_id == membership.project_id)
        .order_by(ComparisonRun.created_at.desc())
    )
    comparison_ids = [row[0] for row in db.execute(stmt)]
    return [_read(db, comparison_id) for comparison_id in comparison_ids]


@router.post("/comparisons", response_model=ComparisonRunRead, status_code=201)
def create_comparison(
    body: ComparisonRunCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Configures 2+ candidates (Section 7.3) -- creates nothing runnable
    yet. See POST /comparisons/{id}/launch."""
    task = _get_task_or_404(db, body.task_id)
    check_project_access(db, user=user, project_id=task.project_id, action=ProjectAction.MODIFY)

    key = idempotency_key or f"auto:{uuid.uuid4()}"
    idempotency = IdempotencyService(db)
    begin = idempotency.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="comparison_run")
    if begin.outcome == BeginOutcome.ALREADY_COMPLETED:
        comparison_id = (begin.key_row.result_ref or {}).get("comparison_id")
        if comparison_id is None:
            raise NotFoundError(f"Idempotency key {key!r} completed with no recorded comparison_id.")
        return _read(db, comparison_id)

    try:
        comparison = ComparisonService(db).create_comparison(
            task_id=body.task_id,
            candidates=[c.model_dump(exclude_none=True) for c in body.candidates],
            budget_id=body.budget_id,
        )
        idempotency.complete(key, result_ref={"comparison_id": comparison.id})
        return _read(db, comparison.id)
    except Exception:
        idempotency.fail(key)
        raise


@router.post("/comparisons/{comparison_id}/launch", response_model=ComparisonRunRead)
def launch_comparison(
    comparison_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Creates one independent Task Run + Agent Run per configured
    candidate and enqueues each as its own job_queue row -- the actual
    dispatch onto MA3's queue/worker plane (Section 10.3)."""
    comparison = _get_comparison_or_404(db, comparison_id)
    _require_comparison_access(db, user, comparison, ProjectAction.MODIFY)

    key = idempotency_key or f"auto:{uuid.uuid4()}"
    idempotency = IdempotencyService(db)
    begin = idempotency.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="comparison_launch")
    if begin.outcome == BeginOutcome.ALREADY_COMPLETED:
        return _read(db, comparison_id)

    try:
        ComparisonService(db).launch_comparison(comparison_id)
        idempotency.complete(key, result_ref={"comparison_id": comparison_id})
        return _read(db, comparison_id)
    except Exception:
        idempotency.fail(key)
        raise


@router.get("/comparisons/{comparison_id}", response_model=ComparisonRunRead)
def get_comparison(comparison_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Consolidated evidence view: every candidate's model/artifact/status/
    tokens/cost/latency, plus comparison-level aggregate tokens/cost and
    the derived phase (Section 18.2/18.3)."""
    comparison = _get_comparison_or_404(db, comparison_id)
    _require_comparison_access(db, user, comparison, ProjectAction.READ)
    return _read(db, comparison_id)


@router.post("/comparisons/{comparison_id}/evaluations", response_model=ComparisonEvaluationResponse)
def analyze_comparison(
    comparison_id: str,
    body: ComparisonEvaluationRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Fans out one independent EvaluationRun per eligible candidate
    (Section: MA6 Slice 3C) -- the domain action behind the future
    "Analyze Results" UI label, never the label/endpoint name itself.
    Reuses exactly the same single-AgentRun create_run/
    create_agent_evaluator_run validation and dispatch as POST
    /agent-runs/{agent_run_id}/evaluations (app.api.routers.
    evaluation_runs) once per eligible candidate -- no second inference
    pipeline, no comparative/ranking evaluator call, no winner. A
    candidate not yet eligible (not launched, not COMPLETED, or no
    resolvable output Artifact) or whose EvaluationRun creation itself
    fails is reported per-candidate rather than aborting its siblings.
    Idempotent: retrying the identical request (same comparison,
    candidate, Evaluation Definition Version, method, and evaluator
    configuration) reuses the prior EvaluationRun instead of creating a
    duplicate -- see app.services.evaluation_execution_service.
    _comparison_evaluation_idempotency_key."""
    comparison = _get_comparison_or_404(db, comparison_id)
    _require_comparison_access(db, user, comparison, ProjectAction.MODIFY)

    outcomes = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison_id,
        evaluation_definition_version_id=body.evaluation_definition_version_id,
        method=body.method,
        evaluator_agent_version_id=body.evaluator_agent_version_id,
        evaluator_model_policy_override=body.evaluator_model_policy_override,
        budget_id=body.budget_id,
        requested_by_user_id=user.id,
    )
    return ComparisonEvaluationResponse(
        comparison_id=comparison_id,
        results=[
            ComparisonCandidateEvaluationResult(
                comparison_candidate_id=o.comparison_candidate_id,
                subject_agent_run_id=o.subject_agent_run_id,
                status=o.status,
                evaluation_run_id=o.evaluation_run_id,
                reason=o.reason,
            )
            for o in outcomes
        ],
    )


@router.post("/comparisons/{comparison_id}/select-winner", response_model=ComparisonRunRead)
def select_comparison_winner(
    comparison_id: str,
    body: SelectWinnerRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Sets is_winner on the corresponding comparison_candidates row and
    syncs the denormalized winner_agent_run_id/winner_artifact_id/
    winner_artifact_hash -- the caller must echo the exact artifact_hash it
    displayed (409 artifact_hash_mismatch otherwise), and the platform
    never picks this on its own (Section 18.3)."""
    comparison = _get_comparison_or_404(db, comparison_id)
    _require_comparison_access(db, user, comparison, ProjectAction.MODIFY)
    ComparisonService(db).select_canonical(
        comparison_id,
        candidate_id=body.comparison_candidate_id,
        artifact_hash=body.artifact_hash,
        selected_by=user.id,
    )
    return _read(db, comparison_id)


@router.post("/comparisons/{comparison_id}/cancel", response_model=ComparisonRunRead)
def cancel_comparison(
    comparison_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Cooperative cancellation (Section 26.7): requests cancellation of
    every in-flight candidate immediately, but the comparison only reaches
    the stored CANCELLED status once every candidate has actually stopped."""
    comparison = _get_comparison_or_404(db, comparison_id)
    _require_comparison_access(db, user, comparison, ProjectAction.MODIFY)
    ComparisonService(db).cancel_comparison(comparison_id, requested_by=user.id)
    return _read(db, comparison_id)

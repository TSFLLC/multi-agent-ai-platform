"""Evaluation Run resource boundary — MA6 Slice 2, extended by Slice 3C,
extended by Slice 3D's read-model detail endpoint.

Manual-trigger only (MA6 V1 invariant): the operator explicitly requests
an evaluation and supplies the Evaluation Definition Version (and, for
AGENT_EVALUATOR, the evaluator Agent Version/model themselves -- never
auto-selected) -- nothing here ever auto-enqueues an evaluation on Agent
Run or MA5 candidate completion. Every real endpoint requires
authentication and project authorization (app.authz), resolved through
the Evaluation Run's subject Agent Run's own Task Run -> Task chain,
exactly like app.api.routers.tasks resolves it for a plain Agent Run.
"""

from dataclasses import asdict
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.authz import ProjectAction, check_project_access
from app.db.enums import EvaluationMethod
from app.errors import NotFoundError
from app.models.evaluation_runs import EvaluationRun
from app.models.identity import User
from app.models.tasks import AgentRun, Task, TaskRun
from app.schemas.evaluation_runs import (
    AgentIdentityRead,
    EvaluationCriterionResultRead,
    EvaluationDefinitionIdentityRead,
    EvaluationRunCreate,
    EvaluationRunDetailRead,
    EvaluationRunRead,
    EvaluationSubjectRead,
    EvaluatorRead,
    ExecutionEvidenceRead,
    ModelIdentityRead,
)
from app.services.evaluation_execution_service import (
    EvaluationExecutionService,
    EvaluationRunDetail,
    get_evaluation_run_detail,
)

router = APIRouter(tags=["evaluation-runs"])


# -- authorization helpers: resolve the owning project through the chain -----


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


def _get_evaluation_run_or_404(db: Session, evaluation_run_id: str) -> EvaluationRun:
    run = db.get(EvaluationRun, evaluation_run_id)
    if run is None:
        raise NotFoundError(f"Evaluation Run {evaluation_run_id} not found.")
    return run


def _project_id_for_evaluation_run(db: Session, run: EvaluationRun) -> str:
    agent_run = _get_agent_run_or_404(db, run.subject_agent_run_id)
    return _project_id_for_agent_run(db, agent_run)


# -- response shaping: Slice 3D read-model detail ---------------------------


def _detail_read(detail: EvaluationRunDetail) -> EvaluationRunDetailRead:
    run = detail.run
    return EvaluationRunDetailRead(
        id=run.id,
        created_at=run.created_at,
        subject_agent_run_id=run.subject_agent_run_id,
        subject_artifact_id=run.subject_artifact_id,
        subject_artifact_content_hash=run.subject_artifact_content_hash,
        evaluation_definition_version_id=run.evaluation_definition_version_id,
        method=run.method,
        status=run.status,
        requested_by_user_id=run.requested_by_user_id,
        evaluator_agent_version_id=run.evaluator_agent_version_id,
        evaluator_task_run_id=run.evaluator_task_run_id,
        evaluator_agent_run_id=run.evaluator_agent_run_id,
        started_at=run.started_at,
        ended_at=run.ended_at,
        failure_reason=run.failure_reason,
        criterion_results=[
            EvaluationCriterionResultRead(
                id=c.id,
                criterion_key=c.criterion_key,
                criterion_label=c.criterion_label,
                criterion_description=c.criterion_description,
                order_index=c.order_index,
                finding=c.finding,
                rationale=c.rationale,
                evidence_refs=c.evidence_refs,
            )
            for c in detail.criterion_results
        ],
        subject=EvaluationSubjectRead(
            agent_run_id=detail.subject.agent_run_id,
            agent=AgentIdentityRead(**asdict(detail.subject.agent)),
            model=ModelIdentityRead(**asdict(detail.subject.model)) if detail.subject.model else None,
            artifact_id=detail.subject.artifact_id,
            artifact_content_hash=detail.subject.artifact_content_hash,
        ),
        evaluation_definition=EvaluationDefinitionIdentityRead(**asdict(detail.evaluation_definition)),
        evaluator=(
            EvaluatorRead(
                agent=AgentIdentityRead(**asdict(detail.evaluator.agent)),
                agent_run_id=detail.evaluator.agent_run_id,
                model=ModelIdentityRead(**asdict(detail.evaluator.model)) if detail.evaluator.model else None,
                execution_evidence=(
                    ExecutionEvidenceRead(**asdict(detail.evaluator.execution_evidence))
                    if detail.evaluator.execution_evidence
                    else None
                ),
            )
            if detail.evaluator
            else None
        ),
    )


# -- routes ---------------------------------------------------------------


@router.post(
    "/agent-runs/{agent_run_id}/evaluations",
    response_model=EvaluationRunRead,
    status_code=201,
)
def create_evaluation_run(
    agent_run_id: str,
    body: EvaluationRunCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    agent_run = _get_agent_run_or_404(db, agent_run_id)
    check_project_access(
        db, user=user, project_id=_project_id_for_agent_run(db, agent_run), action=ProjectAction.MODIFY
    )
    svc = EvaluationExecutionService(db)
    if body.method == EvaluationMethod.AGENT_EVALUATOR:
        # EvaluationRunCreate's own validator already guarantees this is
        # set whenever method=agent_evaluator (422 otherwise) -- narrows
        # Optional[str] -> str for create_agent_evaluator_run's signature.
        assert body.evaluator_agent_version_id is not None
        return svc.create_agent_evaluator_run(
            agent_run_id=agent_run_id,
            subject_artifact_id=body.subject_artifact_id,
            evaluation_definition_version_id=body.evaluation_definition_version_id,
            evaluator_agent_version_id=body.evaluator_agent_version_id,
            evaluator_model_policy_override=body.evaluator_model_policy_override,
            budget_id=body.budget_id,
            requested_by_user_id=user.id,
        )
    return svc.create_run(
        agent_run_id=agent_run_id,
        subject_artifact_id=body.subject_artifact_id,
        evaluation_definition_version_id=body.evaluation_definition_version_id,
        requested_by_user_id=user.id,
    )


@router.get("/agent-runs/{agent_run_id}/evaluations", response_model=List[EvaluationRunRead])
def list_evaluation_runs(
    agent_run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    agent_run = _get_agent_run_or_404(db, agent_run_id)
    check_project_access(
        db, user=user, project_id=_project_id_for_agent_run(db, agent_run), action=ProjectAction.READ
    )
    return EvaluationExecutionService(db).list_runs_for_agent_run(agent_run_id=agent_run_id)


@router.get("/evaluation-runs/{evaluation_run_id}", response_model=EvaluationRunDetailRead)
def get_evaluation_run(
    evaluation_run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Full provenance detail (Section: MA6 Slice 3D) -- subject/evaluator
    Agent/AgentVersion/model identity, evaluation definition identity, and
    aggregated evaluator execution evidence, on top of the same flat
    fields this endpoint already returned (Slice 2/3C). Purely a read: no
    provider/model call, no state change."""
    run = _get_evaluation_run_or_404(db, evaluation_run_id)
    check_project_access(
        db, user=user, project_id=_project_id_for_evaluation_run(db, run), action=ProjectAction.READ
    )
    detail = get_evaluation_run_detail(db, evaluation_run_id)
    assert detail is not None  # _get_evaluation_run_or_404 already confirmed the row exists
    return _detail_read(detail)

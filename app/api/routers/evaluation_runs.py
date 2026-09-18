"""Evaluation Run resource boundary — MA6 Slice 2, extended by Slice 3C.

Manual-trigger only (MA6 V1 invariant): the operator explicitly requests
an evaluation and supplies the Evaluation Definition Version (and, for
AGENT_EVALUATOR, the evaluator Agent Version/model themselves -- never
auto-selected) -- nothing here ever auto-enqueues an evaluation on Agent
Run or MA5 candidate completion. Every real endpoint requires
authentication and project authorization (app.authz), resolved through
the Evaluation Run's subject Agent Run's own Task Run -> Task chain,
exactly like app.api.routers.tasks resolves it for a plain Agent Run.
"""

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
from app.schemas.evaluation_runs import EvaluationRunCreate, EvaluationRunRead
from app.services.evaluation_execution_service import EvaluationExecutionService

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


@router.get("/evaluation-runs/{evaluation_run_id}", response_model=EvaluationRunRead)
def get_evaluation_run(
    evaluation_run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    run = _get_evaluation_run_or_404(db, evaluation_run_id)
    check_project_access(
        db, user=user, project_id=_project_id_for_evaluation_run(db, run), action=ProjectAction.READ
    )
    return run

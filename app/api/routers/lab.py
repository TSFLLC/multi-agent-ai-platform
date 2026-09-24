from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.errors import ConflictError
from app.models.identity import User
from app.models.lab import EvalSetVersionTask, Experiment
from app.schemas.lab import (
    ExperimentConceptBind,
    ExperimentConclusionWrite,
    ExperimentCreate,
    ExperimentEvaluateRequest,
    ExperimentListRead,
    ExperimentRead,
    StarterTestKitsRead,
    TestKitCreate,
    TestKitRead,
    TestKitVersionCreate,
    TestKitVersionRead,
)
from app.services.experiment_execution_service import ExperimentExecutionService
from app.services.experiment_learning_qualification_service import ExperimentLearningQualificationService
from app.services.lab_service import LabService
from app.services.learner_state_service import LearnerStateService

router = APIRouter(prefix="/lab", tags=["personal-lab"])


def _kit_read(kit):
    return {
        "id": kit.id,
        "name": kit.name,
        "description": kit.description,
        "role_term_id": kit.role_term_id,
        "created_at": kit.created_at,
    }


@router.post("/test-kits/starter", response_model=StarterTestKitsRead)
def create_starter_test_kits(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = LabService(db)
    kits, versions = service.starter_kits(user)
    return {
        "test_kits": [_kit_read(kit) for kit in kits],
        "versions": [
            {
                "id": version.id,
                "eval_set_id": version.eval_set_id,
                "version": version.version,
                "status": version.status,
                "task_ids": [
                    row.task_id for row in db.execute(
                        select(EvalSetVersionTask)
                        .where(EvalSetVersionTask.eval_set_version_id == version.id)
                        .order_by(EvalSetVersionTask.position)
                    ).scalars().all()
                ],
                "created_at": version.created_at,
                "published_at": version.published_at,
                "frozen_at": version.frozen_at,
            }
            for version in versions
        ],
    }


@router.get("/test-kits", response_model=List[TestKitRead])
def list_test_kits(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return [_kit_read(kit) for kit in LabService(db).list_kits(user.id)]


@router.post("/test-kits", response_model=TestKitRead, status_code=201)
def create_test_kit(
    body: TestKitCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _kit_read(LabService(db).create_test_kit(user, name=body.name, description=body.description, task_ids=body.task_ids))


@router.post("/test-kits/{kit_id}/versions", response_model=TestKitVersionRead, status_code=201)
def create_test_kit_version(
    kit_id: str,
    body: TestKitVersionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    version = LabService(db).create_version(user, kit_id, body.task_ids)
    return {
        "id": version.id,
        "eval_set_id": version.eval_set_id,
        "version": version.version,
        "status": version.status,
        "task_ids": body.task_ids,
        "created_at": version.created_at,
        "published_at": version.published_at,
        "frozen_at": version.frozen_at,
    }


@router.post("/test-kits/{kit_id}/versions/{version_id}/publish", response_model=TestKitVersionRead)
def publish_test_kit_version(
    kit_id: str,
    version_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    service = LabService(db)
    version = service.publish_version(user, kit_id, version_id)
    task_ids = [row.task_id for row in db.execute(
        select(EvalSetVersionTask).where(EvalSetVersionTask.eval_set_version_id == version.id).order_by(EvalSetVersionTask.position)
    ).scalars()]
    return {
        "id": version.id,
        "eval_set_id": version.eval_set_id,
        "version": version.version,
        "status": version.status,
        "task_ids": task_ids,
        "created_at": version.created_at,
        "published_at": version.published_at,
        "frozen_at": version.frozen_at,
    }


@router.post("/experiments", response_model=ExperimentRead, status_code=201)
def create_experiment(
    body: ExperimentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    service = LabService(db)
    return service.experiment_read(service.create_experiment(user, body))


@router.get("/experiments", response_model=ExperimentListRead)
def list_experiments(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = LabService(db)
    experiments = db.execute(select(Experiment).where(Experiment.user_id == user.id).order_by(Experiment.created_at.desc())).scalars()
    return {"items": [service.experiment_read(experiment) for experiment in experiments]}


@router.get("/experiments/{experiment_id}/results")
def experiment_results(experiment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return ExperimentExecutionService(db).read(user.id, experiment_id)


@router.get("/experiments/{experiment_id}", response_model=ExperimentRead)
def get_experiment(experiment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = LabService(db)
    return service.experiment_read(service.get_experiment(user.id, experiment_id))


@router.put("/experiments/{experiment_id}/conclusion", response_model=ExperimentRead)
def save_experiment_conclusion(
    experiment_id: str,
    body: ExperimentConclusionWrite,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    service = LabService(db)
    return service.experiment_read(
        service.save_conclusion(user.id, experiment_id, body.conclusion_type, body.conclusion_text)
    )


@router.put("/experiments/{experiment_id}/concept", response_model=ExperimentRead)
def bind_experiment_concept(
    experiment_id: str,
    body: ExperimentConceptBind,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    service = LabService(db)
    return service.experiment_read(service.bind_concept(user.id, experiment_id, body.concept_id))


@router.get("/experiments/{experiment_id}/learning-qualification")
def experiment_learning_qualification(
    experiment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    return ExperimentLearningQualificationService(db).assess(user.id, experiment_id)


@router.post("/experiments/{experiment_id}/count-toward-learning")
def count_experiment_toward_learning(
    experiment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    evidence, created, qualification = ExperimentLearningQualificationService(db).count_toward_learning(
        user.id, experiment_id
    )
    if evidence is None:
        raise ConflictError(qualification["message"], detail={"qualification": qualification})
    state = LearnerStateService(db).state(user.id, evidence.concept_id)
    return {
        "created": created,
        "evidence": {
            "id": evidence.id,
            "concept_id": evidence.concept_id,
            "concept_version_id": evidence.concept_version_id,
            "evidence_type": evidence.evidence_type,
            "grader": evidence.grader,
            "passed": evidence.passed,
            "ref_type": evidence.ref_type,
            "ref_id": evidence.ref_id,
            "created_at": evidence.created_at,
        },
        "learner_state": {"ladder": state.ladder, "overlays": sorted(state.overlays)},
        "qualification": qualification,
    }


@router.post("/experiments/{experiment_id}/run")
def run_experiment(experiment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return ExperimentExecutionService(db).read(user.id, ExperimentExecutionService(db).launch(user.id, experiment_id).id)


@router.post("/experiments/{experiment_id}/cancel")
def cancel_experiment(experiment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return ExperimentExecutionService(db).read(user.id, ExperimentExecutionService(db).cancel(user.id, experiment_id).id)


@router.post("/experiments/{experiment_id}/evaluate")
def evaluate_experiment(
    experiment_id: str,
    body: ExperimentEvaluateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return ExperimentExecutionService(db).evaluate(
        user.id,
        experiment_id,
        evaluation_definition_version_id=body.evaluation_definition_version_id,
        method=body.method,
        evaluator_agent_version_id=body.evaluator_agent_version_id,
    )

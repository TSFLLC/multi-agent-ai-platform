from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.models.identity import User
from app.models.lab import EvalSetVersionTask, Experiment
from app.schemas.lab import (
    ExperimentCreate,
    ExperimentListRead,
    ExperimentRead,
    StarterTestKitsRead,
    TestKitCreate,
    TestKitRead,
    TestKitVersionCreate,
    TestKitVersionRead,
)
from app.services.lab_service import LabService

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


@router.get("/experiments/{experiment_id}", response_model=ExperimentRead)
def get_experiment(experiment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = LabService(db)
    return service.experiment_read(service.get_experiment(user.id, experiment_id))

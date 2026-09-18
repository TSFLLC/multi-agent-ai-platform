"""Evaluation Definition Registry resource boundary — MA6 Slice 1.

Structurally mirrors app.api.routers.agents: Evaluation Definitions are
project-scoped (evaluation_definitions.project_id), per the MA6
architecture checkpoint decision — publish/deprecate/retire (consequential)
require ProjectAction.ADMIN; create/list/read require MODIFY/READ
respectively, resolved through app.authz exactly like the Agent Registry,
never an inline role check.

No evaluation-*execution* endpoint exists here (no run/result/score) --
Slice 1 only exposes what a rubric is and how it's versioned.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.authz import ProjectAction, check_project_access, require_project_access
from app.errors import NotFoundError
from app.models.evaluation_definitions import EvaluationDefinition, EvaluationDefinitionVersion
from app.models.identity import User
from app.schemas.evaluation_definitions import (
    EvaluationCriterionCreate,
    EvaluationDefinitionCreate,
    EvaluationDefinitionRead,
    EvaluationDefinitionVersionCreate,
    EvaluationDefinitionVersionRead,
)
from app.services.evaluation_definition_service import EvaluationDefinitionService

router = APIRouter(tags=["evaluation-definitions"])


def _get_definition_or_404(db: Session, evaluation_definition_id: str) -> EvaluationDefinition:
    definition = db.get(EvaluationDefinition, evaluation_definition_id)
    if definition is None:
        raise NotFoundError(f"Evaluation definition {evaluation_definition_id} not found.")
    return definition


def _get_version_or_404(db: Session, evaluation_definition_id: str, version: int) -> EvaluationDefinitionVersion:
    for candidate in EvaluationDefinitionService(db).list_versions(evaluation_definition_id=evaluation_definition_id):
        if candidate.version == version:
            return candidate
    raise NotFoundError(f"Evaluation definition {evaluation_definition_id} has no version {version}.")


@router.get("/evaluation-definitions", response_model=List[EvaluationDefinitionRead])
def list_evaluation_definitions(
    db: Session = Depends(get_db),
    membership=Depends(require_project_access(ProjectAction.READ)),
):
    return EvaluationDefinitionService(db).list_definitions(project_id=membership.project_id)


@router.post("/evaluation-definitions", response_model=EvaluationDefinitionRead, status_code=201)
def create_evaluation_definition(
    body: EvaluationDefinitionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    check_project_access(db, user=user, project_id=body.project_id, action=ProjectAction.MODIFY)
    return EvaluationDefinitionService(db).create_definition(
        project_id=body.project_id, name=body.name, description=body.description
    )


@router.get("/evaluation-definitions/{evaluation_definition_id}", response_model=EvaluationDefinitionRead)
def get_evaluation_definition(
    evaluation_definition_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    definition = _get_definition_or_404(db, evaluation_definition_id)
    check_project_access(db, user=user, project_id=definition.project_id, action=ProjectAction.READ)
    return definition


@router.get(
    "/evaluation-definitions/{evaluation_definition_id}/versions",
    response_model=List[EvaluationDefinitionVersionRead],
)
def list_evaluation_definition_versions(
    evaluation_definition_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    definition = _get_definition_or_404(db, evaluation_definition_id)
    check_project_access(db, user=user, project_id=definition.project_id, action=ProjectAction.READ)
    return EvaluationDefinitionService(db).list_versions(evaluation_definition_id=evaluation_definition_id)


@router.post(
    "/evaluation-definitions/{evaluation_definition_id}/versions",
    response_model=EvaluationDefinitionVersionRead,
    status_code=201,
)
def create_evaluation_definition_version(
    evaluation_definition_id: str,
    body: EvaluationDefinitionVersionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    definition = _get_definition_or_404(db, evaluation_definition_id)
    check_project_access(db, user=user, project_id=definition.project_id, action=ProjectAction.MODIFY)
    return EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=evaluation_definition_id,
        description=body.description,
        criteria=[c.model_dump(exclude_none=True) for c in body.criteria],
    )


@router.post(
    "/evaluation-definitions/{evaluation_definition_id}/versions/{version}/publish",
    response_model=EvaluationDefinitionVersionRead,
)
def publish_evaluation_definition_version(
    evaluation_definition_id: str, version: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Publishing is append-only: never mutates a prior content column
    (Section 12.3 immutability invariant, applied to evaluation rubrics) --
    only flips status."""
    definition = _get_definition_or_404(db, evaluation_definition_id)
    check_project_access(db, user=user, project_id=definition.project_id, action=ProjectAction.ADMIN)
    target = _get_version_or_404(db, evaluation_definition_id, version)
    return EvaluationDefinitionService(db).publish_version(target.id)


@router.post(
    "/evaluation-definitions/{evaluation_definition_id}/versions/{version}/deprecate",
    response_model=EvaluationDefinitionVersionRead,
)
def deprecate_evaluation_definition_version(
    evaluation_definition_id: str, version: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    definition = _get_definition_or_404(db, evaluation_definition_id)
    check_project_access(db, user=user, project_id=definition.project_id, action=ProjectAction.ADMIN)
    target = _get_version_or_404(db, evaluation_definition_id, version)
    return EvaluationDefinitionService(db).deprecate_version(target.id)


@router.post(
    "/evaluation-definitions/{evaluation_definition_id}/versions/{version}/retire",
    response_model=EvaluationDefinitionVersionRead,
)
def retire_evaluation_definition_version(
    evaluation_definition_id: str, version: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    definition = _get_definition_or_404(db, evaluation_definition_id)
    check_project_access(db, user=user, project_id=definition.project_id, action=ProjectAction.ADMIN)
    target = _get_version_or_404(db, evaluation_definition_id, version)
    return EvaluationDefinitionService(db).retire_version(target.id)

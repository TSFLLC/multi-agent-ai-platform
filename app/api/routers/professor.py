"""Bounded learner-facing AI Professor API."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.errors import ConflictError
from app.models.identity import User
from app.schemas.professor import (
    ProfessorContextPreviewRead,
    ProfessorContextRequest,
    ProfessorIntent,
    ProfessorInteractionCreate,
    ProfessorInteractionRead,
    ProfessorTarget,
    ProfessorTargetOptionsRead,
    ProfessorTargetType,
)
from app.services.professor_execution_service import ProfessorExecutionService

router = APIRouter(prefix="/professor", tags=["professor"])


@router.post("/interactions", response_model=ProfessorInteractionRead)
def create_interaction(
    body: ProfessorInteractionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return ProfessorExecutionService(db).create_and_execute(user, body)


@router.get("/targets", response_model=ProfessorTargetOptionsRead)
def target_options(
    intent: ProfessorIntent = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return ProfessorExecutionService(db).target_options(user, intent)


@router.get("/interactions/{interaction_id}", response_model=ProfessorInteractionRead)
def get_interaction(
    interaction_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return ProfessorExecutionService(db).read_interaction(user, interaction_id)


@router.post("/interactions/{interaction_id}/continue", response_model=ProfessorInteractionRead)
def continue_interaction(
    interaction_id: str,
    body: ProfessorContextRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return ProfessorExecutionService(db).continue_interaction(user, interaction_id, body)


@router.get("/context-preview", response_model=ProfessorContextPreviewRead)
def context_preview(
    intent: ProfessorIntent = Query(ProfessorIntent.ASK_PROFESSOR),
    question: Optional[str] = Query(None, max_length=4000),
    target_type: Optional[ProfessorTargetType] = Query(None),
    target_id: Optional[str] = Query(None, max_length=36),
    step_key: Optional[str] = Query(None, max_length=64),
    help: Optional[str] = Query(None, pattern="^(ask|hint)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        target = ProfessorTarget(type=target_type, id=target_id, step_key=step_key) if target_type and target_id else None
        request = ProfessorContextRequest(intent=intent, question=question, target=target, help=help)
    except ValueError as exc:   # e.g. an academy_step target without its step, or help without a step target
        errors = exc.errors() if hasattr(exc, "errors") else []
        raise ConflictError(errors[0]["msg"].replace("Value error, ", "") if errors else "Invalid Professor context request.")
    return ProfessorExecutionService(db).preview(user, request)

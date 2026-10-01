"""AIL.5D.5 — Academy Educational Lab Kit practice API.

Every request body is deliberately tiny. A client names WHICH Day step it is practising and, per run, the values of the
variables the authored scenario permits; it can never name a model, a task, an agent, a budget, a cap, an assistance level,
a status, a result or a return URL (those are server-side), and an unknown field is rejected (``extra=forbid``).
"""

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.models.identity import User
from app.services.academy_practice_service import AcademyPracticeService

router = APIRouter(prefix="/academy/practice", tags=["academy-practice"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PracticeCreateRequest(_Strict):
    learning_item_id: str = Field(min_length=1, max_length=36)
    step_key: str = Field(min_length=1, max_length=64)


class PracticeRunRequest(_Strict):
    variables: Dict[str, str] = Field(default_factory=dict)


class PracticeResponseRequest(_Strict):
    kind: Literal["prediction", "observation", "comparison", "reflection"]
    text: str = Field(max_length=20_000)
    prediction_id: Optional[str] = Field(default=None, max_length=40)
    run_ids: Optional[List[str]] = Field(default=None, max_length=10)


@router.post("", status_code=201)
def create_or_resume(body: PracticeCreateRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Dict[str, Any]:
    return AcademyPracticeService(db).create(user, body.learning_item_id, body.step_key)


@router.get("")
def for_step(learning_item_id: str, step_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Dict[str, Any]:
    return AcademyPracticeService(db).for_step(user.id, learning_item_id, step_key)


@router.get("/{instance_id}")
def read(instance_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Dict[str, Any]:
    return AcademyPracticeService(db).view(user.id, instance_id)


@router.post("/{instance_id}/responses")
def respond(instance_id: str, body: PracticeResponseRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Dict[str, Any]:
    payload = {k: v for k, v in body.model_dump().items() if k != "kind" and v is not None}
    return AcademyPracticeService(db).respond(user, instance_id, body.kind, payload)


@router.post("/{instance_id}/runs", status_code=201)
def run(instance_id: str, body: PracticeRunRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Dict[str, Any]:
    return AcademyPracticeService(db).start_run(user, instance_id, body.variables)


@router.get("/{instance_id}/handoff")
def handoff(instance_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Dict[str, Any]:
    return AcademyPracticeService(db).handoff(user.id, instance_id)


@router.post("/{instance_id}/abandon")
def abandon(instance_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Dict[str, Any]:
    return AcademyPracticeService(db).abandon(user.id, instance_id)

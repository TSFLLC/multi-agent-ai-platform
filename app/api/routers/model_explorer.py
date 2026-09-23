"""Model Explorer / Provider Explorer resource boundary — AIL.1B.

Read-only. Registry facts (``models``/``providers``/``provider_models``/
``model_capabilities``/catalog-history ``provider_model_snapshots``) need
only authentication, same as ``/models``/``/providers`` (MA2) — they carry
no project_id. "Our evidence" is optional and, when a ``project_id`` is
supplied, gated by the same ``check_project_access`` RBAC check MA8.3 uses
(app.api.routers.model_intelligence) so another project's history is never
reachable; omitting ``project_id`` returns facts only, honestly labeled
"no project selected" rather than empty/zero evidence.

No endpoint here writes to ``model_routing_decisions`` or
``router_policy_versions`` — Model Explorer never mutates routing state.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.authz import ProjectAction, check_project_access
from app.auth import get_current_user
from app.models.identity import User
from app.services.model_explorer_service import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_WHATS_NEW_LIMIT,
    MAX_HISTORY_LIMIT,
    MAX_WHATS_NEW_LIMIT,
    ModelExplorerService,
    ProviderExplorerService,
)

router = APIRouter(prefix="/ail", tags=["ail-model-explorer"])


def _check_project(db: Session, user: User, project_id: Optional[str]) -> None:
    """Optional project scoping: when given, the caller must actually have
    read access — never a silent no-op. Reuses the same authorization
    decision as every other project-scoped endpoint (app.authz)."""
    if project_id:
        check_project_access(db, user=user, project_id=project_id, action=ProjectAction.READ)


@router.get("/models")
def list_models(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return ModelExplorerService(db).list_models()


# Registered before /models/{model_id} — a literal path segment must be
# matched before the path-parameter route, or "compare" would be captured
# as model_id and this route would never be reached.
@router.get("/models/compare")
def compare_models(
    model_id: List[str] = Query(..., alias="model_id"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    project_id: Optional[str] = Query(default=None),
):
    if not (2 <= len(model_id) <= 4):
        raise HTTPException(status_code=422, detail="provide between 2 and 4 model_id values")
    _check_project(db, user, project_id)
    return ModelExplorerService(db).compare(model_id, project_id=project_id)


@router.get("/models/{model_id}")
def get_model(
    model_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    project_id: Optional[str] = Query(default=None),
):
    _check_project(db, user, project_id)
    return ModelExplorerService(db).get_model(model_id, project_id=project_id)


@router.get("/models/{model_id}/history")
def get_model_history(
    model_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
    limit: int = Query(default=DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
):
    return ModelExplorerService(db).get_model_history(model_id, limit=limit)


@router.get("/providers")
def list_providers(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return ProviderExplorerService(db).list_providers()


@router.get("/providers/{provider_id}")
def get_provider(provider_id: str, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return ProviderExplorerService(db).get_provider(provider_id)


@router.get("/whats-new")
def whats_new(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
    provider_id: Optional[str] = Query(default=None),
    limit: int = Query(default=DEFAULT_WHATS_NEW_LIMIT, ge=1, le=MAX_WHATS_NEW_LIMIT),
):
    return ModelExplorerService(db).whats_new(provider_id=provider_id, limit=limit)

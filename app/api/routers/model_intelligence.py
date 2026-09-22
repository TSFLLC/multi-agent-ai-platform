"""Model Intelligence resource boundary — MA8.3.

Read-only, project-scoped views over execution, evaluation and routing
history (app.services.model_intelligence_service), plus the one operator
control MA8.3 adds: turning MA8.2 evidence-based routing on or off.

Project reads use ``require_project_access(READ)``, so another project's
history is never reachable. The evidence policy is platform-wide
(``router_policy_versions`` carries no project_id — like the provider/model
registry), so reading its status needs only an authenticated user and
changing it needs ``require_platform_admin``.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.authz import ProjectAction, require_platform_admin, require_project_access
from app.models.identity import User
from app.services.model_intelligence_service import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    ModelIntelligenceService,
)

router = APIRouter(prefix="/model-intelligence", tags=["model-intelligence"])


class EvidenceRoutingUpdate(BaseModel):
    enabled: bool


@router.get("/summary")
def get_summary(
    db: Session = Depends(get_db),
    membership=Depends(require_project_access(ProjectAction.READ)),
):
    return ModelIntelligenceService(db).summary(membership.project_id)


@router.get("/roles")
def get_role_models(
    db: Session = Depends(get_db),
    membership=Depends(require_project_access(ProjectAction.READ)),
):
    return ModelIntelligenceService(db).role_models(membership.project_id)


@router.get("/issues")
def get_recent_issues(
    db: Session = Depends(get_db),
    membership=Depends(require_project_access(ProjectAction.READ)),
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
):
    return ModelIntelligenceService(db).recent_issues(membership.project_id, limit=limit)


@router.get("/routing-decisions")
def list_routing_decisions(
    db: Session = Depends(get_db),
    membership=Depends(require_project_access(ProjectAction.READ)),
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    agent_run_id: Optional[str] = Query(default=None),
):
    return ModelIntelligenceService(db).list_decisions(
        membership.project_id, limit=limit, offset=offset, agent_run_id=agent_run_id
    )


@router.get("/routing-decisions/{decision_id}")
def get_routing_decision(
    decision_id: str,
    db: Session = Depends(get_db),
    membership=Depends(require_project_access(ProjectAction.READ)),
):
    return ModelIntelligenceService(db).get_decision(membership.project_id, decision_id)


@router.get("/evidence-policy")
def get_evidence_policy(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return ModelIntelligenceService(db).policy_status()


@router.post("/evidence-policy")
def set_evidence_policy(
    body: EvidenceRoutingUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_platform_admin),
):
    return ModelIntelligenceService(db).set_evidence_routing(enabled=body.enabled, actor=admin)

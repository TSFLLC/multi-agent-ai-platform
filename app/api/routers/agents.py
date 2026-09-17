"""Agent Registry resource boundary — Section 25.2, 25.5, 12.3/12.4, MA2.

Every real endpoint requires authentication and project authorization
(app.authz) — Agents are project-scoped (agents.project_id), so
publish/deprecate/retire (consequential) require ProjectAction.ADMIN;
create/list/read require MODIFY/READ respectively. Never an inline role
check — always through app.authz.check_project_access or the
require_project_access dependency factory.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.auth import get_current_user
from app.authz import ProjectAction, check_project_access, require_project_access
from app.errors import NotFoundError
from app.models.agents import Agent, AgentVersion
from app.models.identity import User
from app.schemas.agents import (
    AgentCreate,
    AgentRead,
    AgentVersionCreate,
    AgentVersionRead,
    PromptVersionCreate,
    PromptVersionRead,
    ToolGrantCreate,
    ToolRead,
)
from app.services.agent_registry_service import AgentRegistryService

router = APIRouter(tags=["agents"])


def _get_agent_or_404(db: Session, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise NotFoundError(f"Agent {agent_id} not found.")
    return agent


def _get_version_or_404(db: Session, agent_id: str, version: int) -> AgentVersion:
    for candidate in AgentRegistryService(db).list_agent_versions(agent_id=agent_id):
        if candidate.version == version:
            return candidate
    raise NotFoundError(f"Agent {agent_id} has no version {version}.")


@router.get("/agents", response_model=List[AgentRead])
def list_agents(
    db: Session = Depends(get_db),
    membership=Depends(require_project_access(ProjectAction.READ)),
):
    # project_id comes from the query string (no {project_id} path
    # segment on this route), resolved by require_project_access's own
    # `project_id: str` parameter — read it back off the returned
    # membership rather than declaring the same query param twice with
    # conflicting requiredness.
    return AgentRegistryService(db).list_agents(project_id=membership.project_id)


@router.post("/agents", response_model=AgentRead, status_code=201)
def create_agent(
    body: AgentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    check_project_access(db, user=user, project_id=body.project_id, action=ProjectAction.MODIFY)
    return AgentRegistryService(db).create_agent(
        project_id=body.project_id, name=body.name, role=body.role, description=body.description
    )


@router.get("/agents/{agent_id}", response_model=AgentRead)
def get_agent(agent_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    agent = _get_agent_or_404(db, agent_id)
    check_project_access(db, user=user, project_id=agent.project_id, action=ProjectAction.READ)
    return agent


@router.get("/agents/{agent_id}/versions", response_model=List[AgentVersionRead])
def list_agent_versions(agent_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    agent = _get_agent_or_404(db, agent_id)
    check_project_access(db, user=user, project_id=agent.project_id, action=ProjectAction.READ)
    return AgentRegistryService(db).list_agent_versions(agent_id=agent_id)


@router.post("/agents/{agent_id}/versions", response_model=AgentVersionRead, status_code=201)
def create_agent_version(
    agent_id: str,
    body: AgentVersionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    agent = _get_agent_or_404(db, agent_id)
    check_project_access(db, user=user, project_id=agent.project_id, action=ProjectAction.MODIFY)
    return AgentRegistryService(db).create_agent_version(
        agent_id=agent_id,
        name=body.name,
        role=body.role,
        description=body.description,
        prompt_version_id=body.prompt_version_id,
        capabilities=body.capabilities,
        model_policy=body.model_policy.model_dump() if body.model_policy else None,
        default_model_strategy=body.default_model_strategy,
        timeout_seconds=body.timeout_seconds,
    )


@router.post("/agents/{agent_id}/versions/{version}/publish", response_model=AgentVersionRead)
def publish_agent_version(
    agent_id: str, version: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Publishing is append-only: creates no mutation of prior content
    columns (Section 12.3 immutability invariant) — only flips status."""
    agent = _get_agent_or_404(db, agent_id)
    check_project_access(db, user=user, project_id=agent.project_id, action=ProjectAction.ADMIN)
    target = _get_version_or_404(db, agent_id, version)
    return AgentRegistryService(db).publish_agent_version(target.id)


@router.post("/agents/{agent_id}/versions/{version}/deprecate", response_model=AgentVersionRead)
def deprecate_agent_version(
    agent_id: str, version: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    agent = _get_agent_or_404(db, agent_id)
    check_project_access(db, user=user, project_id=agent.project_id, action=ProjectAction.ADMIN)
    target = _get_version_or_404(db, agent_id, version)
    return AgentRegistryService(db).deprecate_agent_version(target.id)


@router.post("/agents/{agent_id}/versions/{version}/retire", response_model=AgentVersionRead)
def retire_agent_version(
    agent_id: str, version: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    agent = _get_agent_or_404(db, agent_id)
    check_project_access(db, user=user, project_id=agent.project_id, action=ProjectAction.ADMIN)
    target = _get_version_or_404(db, agent_id, version)
    return AgentRegistryService(db).retire_agent_version(target.id)


@router.get("/agents/{agent_id}/prompt-versions", response_model=List[PromptVersionRead])
def list_prompt_versions(
    agent_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    agent = _get_agent_or_404(db, agent_id)
    check_project_access(db, user=user, project_id=agent.project_id, action=ProjectAction.READ)
    return AgentRegistryService(db).list_prompt_versions(agent_id=agent_id)


@router.post("/agents/{agent_id}/prompt-versions", response_model=PromptVersionRead, status_code=201)
def create_prompt_version(
    agent_id: str,
    body: PromptVersionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    agent = _get_agent_or_404(db, agent_id)
    check_project_access(db, user=user, project_id=agent.project_id, action=ProjectAction.MODIFY)
    return AgentRegistryService(db).create_prompt_version(
        agent_id=agent_id, content=body.content, created_by=user.id
    )


@router.post(
    "/agents/{agent_id}/versions/{version}/tool-grants", response_model=List[ToolGrantCreate], status_code=201
)
def grant_agent_version_tool(
    agent_id: str, version: int, body: ToolGrantCreate, db: Session = Depends(get_db)
):
    not_implemented()


@router.delete("/agents/{agent_id}/versions/{version}/tool-grants/{tool_id}", status_code=204)
def revoke_agent_version_tool(agent_id: str, version: int, tool_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/tools", response_model=List[ToolRead])
def list_tools(db: Session = Depends(get_db)):
    not_implemented()

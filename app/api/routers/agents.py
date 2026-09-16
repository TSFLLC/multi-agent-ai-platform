"""Agent Registry resource boundary — Section 25.2, 25.5, 12.3/12.4."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
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

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=List[AgentRead])
def list_agents(db: Session = Depends(get_db)):
    not_implemented()


@router.post("/agents", response_model=AgentRead, status_code=201)
def create_agent(
    body: AgentCreate,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    not_implemented()


@router.get("/agents/{agent_id}", response_model=AgentRead)
def get_agent(agent_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/agents/{agent_id}/versions", response_model=List[AgentVersionRead])
def list_agent_versions(agent_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.post("/agents/{agent_id}/versions", response_model=AgentVersionRead, status_code=201)
def create_agent_version(agent_id: str, body: AgentVersionCreate, db: Session = Depends(get_db)):
    not_implemented()


@router.post("/agents/{agent_id}/versions/{version}/publish", response_model=AgentVersionRead)
def publish_agent_version(agent_id: str, version: int, db: Session = Depends(get_db)):
    """Publishing is append-only: creates no mutation of prior content
    columns (Section 12.3 immutability invariant) — only flips status."""
    not_implemented()


@router.get("/agents/{agent_id}/prompt-versions", response_model=List[PromptVersionRead])
def list_prompt_versions(agent_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.post("/agents/{agent_id}/prompt-versions", response_model=PromptVersionRead, status_code=201)
def create_prompt_version(agent_id: str, body: PromptVersionCreate, db: Session = Depends(get_db)):
    not_implemented()


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

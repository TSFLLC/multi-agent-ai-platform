"""Starter Agent catalog seeding — MA2, Section 12.5.

The six starter Agents are seed data, not hard-coded platform behavior —
nothing outside this module and its idempotent seeding function knows
these six names/roles. Users create further Agents through the normal
Agent Registry API; this only guarantees the initial six exist.

Idempotent by construction, same pattern as app.bootstrap: fixed,
well-known IDs, so a restart resolves existing rows instead of minting
duplicates. Each starter Agent gets a first Prompt Version and a
published (ACTIVE) Agent Version wrapping it, so it's immediately usable
without an extra publish step — MA2 explicitly says "do not over-engineer
their prompts," so each is a short, direct role statement, deliberately
not tuned.
"""

from typing import List, NamedTuple, Optional

from sqlalchemy.orm import Session

from app.db.enums import VersionStatus
from app.models.agents import Agent, AgentVersion, PromptVersion


class StarterAgentSpec(NamedTuple):
    id: str
    name: str
    role: str
    description: str
    prompt: str


STARTER_AGENTS: List[StarterAgentSpec] = [
    StarterAgentSpec(
        id="00000000-0000-0000-0000-000000000101",
        name="Planner",
        role="planner",
        description="Breaks a task down into a concrete, ordered execution plan.",
        prompt="You are the Planner. Break the given task into a clear, ordered list of concrete steps.",
    ),
    StarterAgentSpec(
        id="00000000-0000-0000-0000-000000000102",
        name="Software Engineer",
        role="software_engineer",
        description="Implements code changes for a given task.",
        prompt="You are the Software Engineer. Implement the requested change correctly and minimally.",
    ),
    StarterAgentSpec(
        id="00000000-0000-0000-0000-000000000103",
        name="Test Engineer",
        role="test_engineer",
        description="Writes and validates tests for a change.",
        prompt="You are the Test Engineer. Write tests that verify the requested behavior.",
    ),
    StarterAgentSpec(
        id="00000000-0000-0000-0000-000000000104",
        name="Code Reviewer",
        role="code_reviewer",
        description="Reviews a change for correctness, clarity, and maintainability.",
        prompt="You are the Code Reviewer. Review the given change for correctness and quality.",
    ),
    StarterAgentSpec(
        id="00000000-0000-0000-0000-000000000105",
        name="Security Reviewer",
        role="security_reviewer",
        description="Reviews a change for security issues.",
        prompt="You are the Security Reviewer. Review the given change for security vulnerabilities.",
    ),
    StarterAgentSpec(
        id="00000000-0000-0000-0000-000000000106",
        name="Research Agent",
        role="research_agent",
        description="Performs research and summarizes findings.",
        prompt="You are the Research Agent. Research the given question and summarize findings clearly.",
    ),
]


def ensure_starter_agents(db: Session, *, project_id: str) -> List[Agent]:
    agents = []
    for spec in STARTER_AGENTS:
        agent = _ensure_one(db, project_id=project_id, spec=spec)
        agents.append(agent)
    db.commit()
    return agents


def _ensure_one(db: Session, *, project_id: str, spec: StarterAgentSpec) -> Agent:
    agent: Optional[Agent] = db.get(Agent, spec.id)
    if agent is not None:
        return agent

    agent = Agent(
        id=spec.id, project_id=project_id, name=spec.name, role=spec.role, description=spec.description
    )
    db.add(agent)
    db.flush()

    prompt_version = PromptVersion(agent_id=agent.id, version=1, content=spec.prompt)
    db.add(prompt_version)
    db.flush()

    agent_version = AgentVersion(
        agent_id=agent.id,
        version=1,
        name=spec.name,
        role=spec.role,
        description=spec.description,
        prompt_version_id=prompt_version.id,
        status=VersionStatus.ACTIVE,
        published_at=prompt_version.created_at,
    )
    db.add(agent_version)
    db.flush()

    agent.current_status = VersionStatus.ACTIVE
    return agent

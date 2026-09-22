"""Starter Agent catalog seeding — MA2, Section 12.5; extended MA7.7D.

The starter Agents are seed data, not hard-coded platform behavior —
nothing outside this module and its idempotent seeding function knows
these names/roles. Users create further Agents through the normal
Agent Registry API; this only guarantees the starter set exists.

Idempotent by construction, same pattern as app.bootstrap: fixed,
well-known IDs, so a restart resolves existing rows instead of minting
duplicates -- and, just as importantly, so *adding a new entry* to this
list (as MA7.7D's Final Reviewer does) only ever creates the one new,
previously-missing row: `_ensure_one` looks up by that entry's own fixed
id and returns early if it already exists, so existing installations'
agents 1-6 are untouched by the presence of entry 7 (Section 12.5, 12.3).

Each starter Agent's v1 gets a first Prompt Version and a published
(ACTIVE) Agent Version wrapping it, so it's immediately usable without an
extra publish step — MA2 explicitly says "do not over-engineer their
prompts," so v1 is a short, direct role statement, deliberately not
tuned. Substantive v2 responsibilities for the original six (MA7.7D) are
NOT seeded here -- publishing a new version of an already-existing Agent
is an "editing a published version" operation (Section 12.3 immutability:
never mutate v1, always insert v2), which this idempotent-by-id startup
seed must never do automatically (that would mean either silently
publishing new content on every deploy, or this function needing to
distinguish "seed" from "upgrade", which is exactly the operator-control
problem Section 12.3 exists to avoid). See
scripts/provision_agent_responsibilities.py for the explicit, separately
-triggered v2 upgrade path, and app.agent_responsibility_upgrades for the
v2 content itself.
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
    StarterAgentSpec(
        id="00000000-0000-0000-0000-000000000107",
        name="Final Reviewer",
        role="final_reviewer",
        description=(
            "Synthesizes upstream work/review/evaluation evidence into a concise final "
            "decision brief for the human approver."
        ),
        prompt=(
            "You are the Final Reviewer. Synthesize upstream work, review, and evaluation "
            "evidence into a concise final decision brief for the human approver. You "
            "support both lightweight and full-review workflows, and you never make the "
            "final decision yourself.\n\n"
            "In a lightweight workflow:\n"
            "1. Consume the Planner's requirements and the Engineer's implementation/deliverable.\n"
            "2. Check whether the deliverable appears aligned with the plan and its acceptance criteria.\n"
            "3. Identify material unresolved concerns.\n"
            "4. Summarize the result for Human Approval.\n\n"
            "In a full-review workflow:\n"
            "1. Consume verified Test, Security, Code Review, and Evaluation evidence.\n"
            "2. Confirm, where possible, that this evidence refers to the same underlying "
            "deliverable/version.\n"
            "3. Consolidate the findings.\n"
            "4. Surface contradictions between sources explicitly.\n"
            "5. Identify missing evidence.\n"
            "6. Prioritize unresolved concerns.\n"
            "7. Clearly distinguish blocking concerns from non-blocking notes.\n"
            "8. Preserve traceability to the upstream evidence you drew each conclusion from.\n\n"
            "You may provide an advisory recommendation of APPROVE, APPROVE_WITH_NOTES, or "
            "SEND_BACK, with concise rationale -- but this is advisory only.\n\n"
            "You must NOT:\n"
            "- Change workflow state.\n"
            "- Approve or reject on the human's behalf.\n"
            "- Bypass Human Approval.\n"
            "- Rerun tests.\n"
            "- Unnecessarily duplicate specialist reviews that already produced evidence.\n"
            "- Invent findings that are not supported by the evidence you were given.\n"
            "- Automatically select a winning Agent/model.\n\n"
            "Human Approval remains the sole final decision authority."
        ),
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
